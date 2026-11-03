"""The purchase register (V2 P4): one row per approved invoice of a month, in the shape a CA
sets beside the GSTR-2B download.

A month is the invoices *dated* in it (the invoice date on the bill, a calendar date: India's
day, never shifted by a timezone) whose newest version was approved, credit and debit notes
apart (they are not read today). Each row is the bill as signed - its corrected values: the
supplier's GSTIN, the name the workspace's books give it (its mapping, V2 P1) or else the
printed name, the invoice number and date, the place of supply's 2-digit code, the taxable
value, CGST, SGST and IGST, their total, and the invoice total - however many lines and
batches the bill has.

The last column notes what DocForge's own checks found ("GSTIN format issue", "Possible
duplicate", "E-invoice QR not verified", ...): data for the CA to weigh, never a judgement of
eligibility for input tax credit. Reverse charge is not read from bills, so it is left blank.

`compare_2b` sets the register beside a GSTR-2B download (`docforge.gstr2b`): by supplier
GSTIN and invoice number (compared as duplicates are: case and spaces aside), each amount
within 1 rupee in exact decimals. A comparison for the user, not a reconciliation with the
portal and no filing.
"""

import calendar
import hashlib
import io
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from docforge import audit
from docforge.db.models import (
    Document,
    DocumentVersion,
    EinvoiceCheck,
    Review,
    SupplierMapping,
    Tenant,
    live,
)
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.gstin import is_valid_gstin
from docforge.gstr2b import TwoBInvoice
from docforge.mappings.keys import gstin_key
from docforge.review.flags import repeated_documents
from docforge.trust.duplicates import text_key

_INDIA = ZoneInfo("Asia/Kolkata")
MAX_INVOICES = 20_000  # a month of the busiest distributor, twice over
TOLERANCE = Decimal("1")  # rupees, for the 2B comparison
HEADERS = (
    "Supplier GSTIN", "Supplier Name", "Invoice Number", "Invoice Date",
    "Place of Supply (code)", "Taxable Value", "CGST", "SGST", "IGST", "Total Tax",
    "Invoice Total", "Reverse Charge", "ITC Eligibility Note",
)  # fmt: skip
ABOUT = "Generated from invoices approved in DocForge. Verify against GSTR-2B before filing."
NOTE_HELP = (
    "ITC Eligibility Note: data flags from DocForge's checks, for you to decide on. It is not "
    "a judgement of eligibility for input tax credit and not tax advice."
)
EXCLUDED = "Credit and debit notes are not included: DocForge does not read them yet."
REVERSE_CHARGE = "Reverse Charge: not read from bills, so left blank."
CHECK_NOTE = (
    "A comparison with the GSTR-2B file you uploaded, by supplier GSTIN and invoice number, "
    "each amount within 1 rupee. Not a reconciliation with the GST portal."
)
_UNVERIFIED_QR = ("bad_signature", "malformed", "unknown_key", "unreadable")
_MONEY = "#,##0.00"
_DATE = "dd-mmm-yyyy"
_CENT = Decimal("0.01")
Status = Literal["matched", "amount_differs", "docforge_only", "twob_only"]
FIELDS = ("taxable_value", "cgst", "sgst", "igst", "invoice_total")
_STATUS_WORDS = {
    "matched": "Matched",
    "amount_differs": "Amount differs",
    "docforge_only": "In DocForge only",
    "twob_only": "In 2B only",
}


class RegisterEmpty(LookupError):
    """No approved invoice is dated in the month (for that supplier)."""


class RegisterInvalid(ValueError):
    """A month, supplier or size the register cannot take, said in words."""


@dataclass(frozen=True)
class RegisterRow:
    document_id: uuid.UUID
    supplier_gstin: str | None
    supplier_name: str | None
    invoice_no: str | None
    invoice_date: date | None
    place_of_supply: str | None
    taxable_value: Decimal | None
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    invoice_total: Decimal | None
    note: str  # "" when the checks found nothing

    @property
    def total_tax(self) -> Decimal:
        return self.cgst + self.sgst + self.igst


@dataclass(frozen=True)
class Totals:
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total_tax: Decimal
    invoice_total: Decimal


@dataclass(frozen=True)
class Register:
    month: date  # its first day
    supplier_gstin: str | None
    rows: tuple[RegisterRow, ...]
    totals: Totals


@dataclass(frozen=True)
class Matched:
    """One register row, or one 2B invoice the register does not have, compared."""

    status: Status
    row: RegisterRow | None
    twob: TwoBInvoice | None
    differences: dict[str, Decimal]  # register less 2B, for each amount beyond 1 rupee


@dataclass(frozen=True)
class TwoBComparison:
    rows: tuple[Matched, ...]  # the register's rows, in its order
    twob_only: tuple[Matched, ...]  # 2B invoices with no approved bill in the register

    def count(self, status: Status) -> int:
        found = self.twob_only if status == "twob_only" else self.rows
        return sum(1 for m in found if m.status == status)


def month_bounds(month: str) -> tuple[date, date]:
    """`2026-11` as its first and last day. Raises `RegisterInvalid`."""
    try:
        year, number = (int(part) for part in month.split("-"))
        first = date(year, number, 1)
    except ValueError as error:
        raise RegisterInvalid("Choose a month as YYYY-MM, such as 2026-11.") from error
    if not 2000 <= year <= 2100:
        raise RegisterInvalid("Choose a month from 2000 to 2100.")
    return first, date(year, number, calendar.monthrange(year, number)[1])


def month_name(first: date) -> str:
    return first.strftime("%B %Y")


def this_month() -> str:
    """The current month in India."""
    return datetime.now(_INDIA).strftime("%Y-%m")


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _sum(values: Sequence[Decimal | None]) -> Decimal:
    return sum((v for v in values if v is not None), Decimal(0)).quantize(_CENT)


def _totals(rows: Sequence[RegisterRow]) -> Totals:
    return Totals(
        taxable_value=_sum([r.taxable_value for r in rows]),
        cgst=_sum([r.cgst for r in rows]),
        sgst=_sum([r.sgst for r in rows]),
        igst=_sum([r.igst for r in rows]),
        total_tax=_sum([r.total_tax for r in rows]),
        invoice_total=_sum([r.invoice_total for r in rows]),
    )


def _path(*keys: str) -> Any:
    return Review.data[("record", *keys)].astext


def _note(
    gstin: str | None, code: str | None, taxable: Decimal | None, total: Decimal | None,
    duplicate: bool, qr: str | None,
) -> str:  # fmt: skip
    flags = []
    if not gstin or not is_valid_gstin(gstin.strip().upper()):
        flags.append("GSTIN format issue")
    if duplicate:
        flags.append("Possible duplicate")
    if qr in _UNVERIFIED_QR:
        flags.append("E-invoice QR not verified")
    if not code:
        flags.append("Place of supply missing")
    if taxable is None or total is None:
        flags.append("Amounts missing")
    return "; ".join(flags)


def _amounts(row: RegisterRow | None, twob: TwoBInvoice | None) -> dict[str, Decimal | None]:
    if row is not None:
        return {"taxable_value": row.taxable_value, "cgst": row.cgst, "sgst": row.sgst,
                "igst": row.igst, "invoice_total": row.invoice_total}  # fmt: skip
    if twob is None:
        return dict.fromkeys(FIELDS)
    return {"taxable_value": twob.taxable_value, "cgst": twob.cgst, "sgst": twob.sgst,
            "igst": twob.igst, "invoice_total": twob.invoice_value}  # fmt: skip


def compare_2b(register: Register, twob: Sequence[TwoBInvoice]) -> TwoBComparison:
    """Each register row against the 2B invoice of the same supplier GSTIN and number:
    matched when every amount is within 1 rupee, else the amounts that differ and by how
    much; a row with none is in DocForge only; a 2B invoice with no row, in 2B only."""
    by_key = {invoice.keys: invoice for invoice in twob}
    used: set[tuple[str, str]] = set()
    rows = []
    for row in register.rows:
        key = (text_key(row.supplier_gstin) or "", text_key(row.invoice_no) or "")
        found = by_key.get(key) if all(key) else None
        if found is None:
            rows.append(Matched("docforge_only", row, None, {}))
            continue
        used.add(key)
        ours, theirs = _amounts(row, None), _amounts(None, found)
        differences = {}
        for name in FIELDS:
            mine, other = ours[name], theirs[name]
            if mine is None or other is None:
                if mine != other:
                    differences[name] = (mine or Decimal(0)) - (other or Decimal(0))
            elif abs(mine - other) > TOLERANCE:
                differences[name] = mine - other
        rows.append(Matched("amount_differs" if differences else "matched", row, found,
                            differences))  # fmt: skip
    supplier = text_key(register.supplier_gstin) if register.supplier_gstin else None
    extra = tuple(
        Matched("twob_only", None, invoice, {})
        for invoice in twob
        if invoice.keys not in used and (supplier is None or invoice.keys[0] == supplier)
    )
    return TwoBComparison(tuple(rows), extra)


class PurchaseRegisterService:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @scoped
    def build(
        self, tenant_id: uuid.UUID, month: str, *, supplier_gstin: str | None = None
    ) -> Register:
        """The month's register (the same rows the preview shows and the file holds).
        Raises `RegisterInvalid`."""
        first, last = month_bounds(month)
        supplier = None
        if supplier_gstin and supplier_gstin.strip():
            supplier = gstin_key(supplier_gstin)
            if supplier is None or not is_valid_gstin(supplier):
                raise RegisterInvalid(
                    f"'{supplier_gstin.strip()}' is not a valid GSTIN: check its 15 characters."
                )
        with self._sessions() as session:
            found = _approved(session, tenant_id, first, last)
            if supplier is not None:
                found = [r for r in found if gstin_key(r[2]) == supplier]
            repeats = repeated_documents(session, tenant_id) if found else set()
            gstins = {key for r in found if (key := gstin_key(r[2]))}
            mappings = (
                session.scalars(
                    select(SupplierMapping).where(
                        SupplierMapping.tenant_id == tenant_id,
                        SupplierMapping.seller_gstin.in_(gstins),
                    )
                ).all()
                if gstins
                else []
            )
            names = {m.seller_gstin: m.tally_ledger or m.marg_party for m in mappings}
        rows = []
        for (document_id, number, gstin, printed, day, code, taxable, cgst, sgst, igst, total,
             qr) in found:  # fmt: skip
            key = gstin_key(gstin)
            taxable_value, invoice_total = _decimal(taxable), _decimal(total)
            rows.append(
                RegisterRow(
                    document_id=document_id,
                    supplier_gstin=key or gstin,
                    supplier_name=(names.get(key) if key else None) or printed,
                    invoice_no=number,
                    invoice_date=_day(day),
                    place_of_supply=code,
                    taxable_value=taxable_value,
                    cgst=_decimal(cgst) or Decimal(0),
                    sgst=_decimal(sgst) or Decimal(0),
                    igst=_decimal(igst) or Decimal(0),
                    invoice_total=invoice_total,
                    note=_note(key, code, taxable_value, invoice_total, document_id in repeats, qr),
                )
            )
        return Register(first, supplier, tuple(rows), _totals(rows))

    @scoped
    def workbook(
        self,
        tenant_id: uuid.UUID,
        month: str,
        *,
        supplier_gstin: str | None = None,
        twob: Sequence[TwoBInvoice] | None = None,
        actor: str,
        person: str,
    ) -> tuple[str, bytes, Register]:
        """The register as an Excel file (and its name): the "Purchase Register" sheet with
        a totals row, an "About" sheet and, given a 2B download, a "2B check" sheet. Raises
        `RegisterInvalid` or, with no approved invoice in the month, `RegisterEmpty`."""
        register = self.build(tenant_id, month, supplier_gstin=supplier_gstin)
        if not register.rows:
            who = f" from {register.supplier_gstin}" if register.supplier_gstin else ""
            raise RegisterEmpty(
                f"No approved invoice{who} is dated in {month_name(register.month)}, so there "
                "is no register to make: approve the month's bills first, or choose another "
                "month."
            )
        with self._sessions() as session:
            tenant = session.get(Tenant, tenant_id)
            workspace = (tenant.display_name or tenant.name) if tenant else str(tenant_id)
        comparison = compare_2b(register, twob) if twob is not None else None
        data = write_workbook(register, workspace=workspace, person=person,
                              comparison=comparison)  # fmt: skip
        sha256 = hashlib.sha256(data).hexdigest()
        label = register.month.strftime("%Y-%m")
        with self._sessions.begin() as session:
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="register.exported",
                target_type="purchase_register", target_id=label,
                details={
                    "month": label, "invoices": len(register.rows),
                    "supplier_filter": register.supplier_gstin is not None,
                    "with_2b": comparison is not None, "sha256": sha256,
                },
            )  # fmt: skip
        supplier = f"-{register.supplier_gstin}" if register.supplier_gstin else ""
        return f"docforge-purchase-register-{label}{supplier}.xlsx", data, register

    @scoped
    def check_2b(
        self,
        tenant_id: uuid.UUID,
        month: str,
        twob: Sequence[TwoBInvoice],
        *,
        supplier_gstin: str | None = None,
        actor: str,
    ) -> tuple[Register, TwoBComparison]:
        """The month's register beside a 2B download. Audited by its counts only."""
        register = self.build(tenant_id, month, supplier_gstin=supplier_gstin)
        comparison = compare_2b(register, twob)
        label = register.month.strftime("%Y-%m")
        with self._sessions.begin() as session:
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="register.compared_2b",
                target_type="purchase_register", target_id=label,
                details={
                    "month": label, "matched": comparison.count("matched"),
                    "amount_differs": comparison.count("amount_differs"),
                    "docforge_only": comparison.count("docforge_only"),
                    "twob_only": comparison.count("twob_only"),
                },
            )  # fmt: skip
        return register, comparison


def _approved(session: Session, tenant_id: uuid.UUID, first: date, last: date) -> list[Any]:
    """The values the register needs of each live invoice dated in the month whose newest
    version was approved, by date then number; never the whole record."""
    newer = aliased(DocumentVersion)
    newest = (
        select(func.max(newer.version_no))
        .where(newer.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )
    qr = (
        select(func.min(EinvoiceCheck.status))
        .where(
            EinvoiceCheck.tenant_id == tenant_id,
            EinvoiceCheck.document_version_id == DocumentVersion.id,
        )
        .correlate(DocumentVersion)
        .scalar_subquery()
    )
    day = _path("invoice_date", "value")
    rows = session.execute(
        select(
            Document.id, _path("invoice_no", "value"), _path("seller", "gstin", "value"),
            _path("seller", "name", "value"), day, _path("place_of_supply_code", "value"),
            _path("totals", "taxable_value", "value"), _path("totals", "cgst", "value"),
            _path("totals", "sgst", "value"), _path("totals", "igst", "value"),
            _path("totals", "grand_total", "value"), qr,
        )
        .join(DocumentVersion, DocumentVersion.id == Review.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Review.tenant_id == tenant_id,
            Document.tenant_id == tenant_id,
            Review.outcome == "approved",
            Document.doc_type == "invoice",
            live(),
            DocumentVersion.version_no == newest,
            day >= first.isoformat(),  # ISO dates compare as text
            day <= last.isoformat(),
        )
        .order_by(day, _path("invoice_no", "value"), Document.id)
        .limit(MAX_INVOICES + 1)
    ).all()  # fmt: skip
    if len(rows) > MAX_INVOICES:
        raise RegisterInvalid(
            f"More than {MAX_INVOICES:,} approved invoices are dated in that month: choose one "
            "supplier at a time."
        )
    return list(rows)


# The workbook


def write_workbook(
    register: Register,
    *,
    workspace: str,
    person: str,
    comparison: TwoBComparison | None = None,
    now: datetime | None = None,
) -> bytes:
    """Streamed (openpyxl's write-only mode): money as numbers with two decimals, dates as
    dates shown dd-mmm-yyyy, GSTINs, numbers and names as text - never a formula."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font

    book = Workbook(write_only=True)
    bold = Font(bold=True)

    def text(sheet: Any, value: str | None, *, strong: bool = False) -> Any:
        cell = WriteOnlyCell(sheet, value=value or None)
        if value:
            cell.data_type = "s"  # text, even "=..." or "007": never read as anything else
        if strong:
            cell.font = bold
        return cell

    def money(sheet: Any, value: Decimal | None, *, strong: bool = False) -> Any:
        cell = WriteOnlyCell(sheet, value=None if value is None else value.quantize(_CENT))
        cell.number_format = _MONEY
        if strong:
            cell.font = bold
        return cell

    def day(sheet: Any, value: date | None) -> Any:
        cell = WriteOnlyCell(sheet, value=value)
        cell.number_format = _DATE
        return cell

    sheet = book.create_sheet("Purchase Register")
    sheet.append([text(sheet, h, strong=True) for h in HEADERS])
    for r in register.rows:
        sheet.append([
            text(sheet, r.supplier_gstin), text(sheet, r.supplier_name),
            text(sheet, r.invoice_no), day(sheet, r.invoice_date),
            text(sheet, r.place_of_supply), money(sheet, r.taxable_value),
            money(sheet, r.cgst), money(sheet, r.sgst), money(sheet, r.igst),
            money(sheet, r.total_tax), money(sheet, r.invoice_total), text(sheet, None),
            text(sheet, r.note),
        ])  # fmt: skip
    t = register.totals
    sheet.append([
        text(sheet, "Total", strong=True), None, None, None, None,
        money(sheet, t.taxable_value, strong=True), money(sheet, t.cgst, strong=True),
        money(sheet, t.sgst, strong=True), money(sheet, t.igst, strong=True),
        money(sheet, t.total_tax, strong=True), money(sheet, t.invoice_total, strong=True),
    ])  # fmt: skip

    about = book.create_sheet("About")
    made = (now or datetime.now(_INDIA)).astimezone(_INDIA)
    for label, value in (
        ("Month", month_name(register.month)),
        ("Workspace", workspace),
        ("Generated at", made.strftime("%d %b %Y, %H:%M IST")),
        ("Generated by", person),
        ("Invoice count", str(len(register.rows))),
        ("Supplier", register.supplier_gstin or "All suppliers"),
    ):
        about.append([text(about, label, strong=True), text(about, value)])
    about.append([])
    for sentence in (ABOUT, NOTE_HELP, EXCLUDED, REVERSE_CHARGE):
        about.append([text(about, sentence)])

    if comparison is not None:
        check = book.create_sheet("2B check")
        check.append([text(check, h, strong=True) for h in (
            "Status", "Supplier GSTIN", "Invoice Number", "Invoice Total (DocForge)",
            "Invoice Value (2B)", "Taxable Value difference", "CGST difference",
            "SGST difference", "IGST difference", "Invoice Total difference",
        )])  # fmt: skip
        for m in (*comparison.rows, *comparison.twob_only):
            source = m.row if m.row is not None else m.twob
            check.append([
                text(check, _STATUS_WORDS[m.status]),
                text(check, source.supplier_gstin if source else None),
                text(check, source.invoice_no if source else None),
                money(check, m.row.invoice_total if m.row else None),
                money(check, m.twob.invoice_value if m.twob else None),
                *(money(check, m.differences.get(f)) for f in FIELDS),
            ])  # fmt: skip
        check.append([])
        check.append([text(check, CHECK_NOTE)])

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
