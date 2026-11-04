"""The PO Variance Report (V2 P7): a month's invoices against the purchase orders they were
billed against, in rupees, by supplier.

A month is the invoices *dated* in it (the bill's own date, a calendar date), as the purchase
register takes them. An invoice is **checked** when its newest version was approved (its
values are the signed record, as corrected) and it is paired with a purchase order (the
match's newest pair; the order's values as corrected). Every other invoice dated in the month
is listed as **not checked**, with its reason - no purchase order linked, not approved yet,
rejected, its order not readable - and counted, never hidden.

Per checked invoice, `trust.variance.compare` with the workspace's tolerances; per supplier,
the sums of its invoices' rows (so the summary always equals the rows it sums). The Excel file
holds numbers as numbers and every text as text (never a formula); the export is audited by
its counts and the file's SHA-256, never a value from a bill.
"""

import hashlib
import io
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from docforge import audit
from docforge.db.models import (
    Correction,
    Document,
    DocumentVersion,
    Extraction,
    MatchRecord,
    Review,
    SupplierMapping,
    Tenant,
    live,
)
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.extraction.normalize import parse_date
from docforge.extraction.purchase_order import (
    PurchaseOrderExtraction,
    RawPurchaseOrder,
    normalize_purchase_order,
)
from docforge.extraction.schema import InvoiceExtraction
from docforge.gstin import is_valid_gstin
from docforge.mappings.keys import gstin_key
from docforge.parsing.base import Page, ParsedDocument
from docforge.purchase_register import RegisterInvalid, month_bounds, month_name
from docforge.review.revise import Correction as Corrected
from docforge.review.revise import apply_corrections
from docforge.trust.variance import LABELS, Tolerances, Variance, compare, money
from docforge.workspace_settings import variance_tolerances

_INDIA = ZoneInfo("Asia/Kolkata")
MAX_INVOICES = 20_000  # as the purchase register
_ZERO = Decimal("0.00")
_MONEY = "#,##0.00"
_PERCENT = "0.00"
_DATE = "dd-mmm-yyyy"
NO_ORDER = "No purchase order linked"
NOT_APPROVED = "Not approved yet: its values are not confirmed"
REJECTED = "Rejected in review"
ORDER_UNREADABLE = "Its purchase order could not be read"
SIGN_NOTE = (
    "Money impact: a positive amount is the bill costing more than the PO agreed (or scheme "
    "goods short); a negative one, less. Lines not in the PO and PO lines not billed are "
    "listed apart and are not in the net."
)
ABOUT = (
    "Generated from invoices approved in DocForge, each against the purchase order it was "
    "matched with. It informs a person; it contacts no supplier and raises no claim."
)
SCOPE = (
    "Checked: approved invoices dated in the month with a purchase order linked. Every other "
    "invoice dated in the month is on the 'Not checked' sheet, with its reason."
)


class VarianceEmpty(LookupError):
    """No invoice is dated in the month (for that supplier)."""


@dataclass(frozen=True)
class Checked:
    document_id: uuid.UUID
    order_document_id: uuid.UUID
    invoice_date: date | None
    supplier_name: str | None  # the books' name (mapping) or as printed
    variance: Variance


@dataclass(frozen=True)
class NotChecked:
    document_id: uuid.UUID
    invoice_no: str | None
    invoice_date: date | None
    supplier_gstin: str | None
    supplier_name: str | None
    reason: str


@dataclass(frozen=True)
class SupplierSummary:
    """A supplier's month: every figure the sum of its invoices' rows."""

    supplier_gstin: str | None
    supplier_name: str | None
    invoices_checked: int
    lines_checked: int
    lines_matched: int
    variances: int
    not_compared: int
    not_in_po_lines: int
    not_billed_lines: int
    net_impact: Decimal
    above_po: Decimal

    @staticmethod
    def of(gstin: str | None, name: str | None, rows: Iterable[Checked]) -> "SupplierSummary":
        found = [r.variance for r in rows]
        return SupplierSummary(
            supplier_gstin=gstin,
            supplier_name=name,
            invoices_checked=len(found),
            lines_checked=sum(len(v.lines) for v in found),
            lines_matched=sum(v.matched_lines for v in found),
            variances=sum(v.variances for v in found),
            not_compared=sum(v.not_compared for v in found),
            not_in_po_lines=sum(len(v.not_in_po) for v in found),
            not_billed_lines=sum(len(v.not_billed) for v in found),
            net_impact=money(sum((v.net_impact for v in found), _ZERO)),
            above_po=money(sum((v.above_po for v in found), _ZERO)),
        )


@dataclass(frozen=True)
class PeriodReport:
    month: date  # its first day
    supplier_gstin: str | None
    tolerances: Tolerances
    checked: tuple[Checked, ...]
    not_checked: tuple[NotChecked, ...]
    suppliers: tuple[SupplierSummary, ...]  # the most charged above the PO first
    totals: SupplierSummary


def _record[T: (InvoiceExtraction, PurchaseOrderExtraction)](
    schema: type[T], data: Any
) -> T | None:
    try:
        return schema.model_validate(data)
    except ValidationError:
        return None


class VarianceReportService:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @scoped
    def build(
        self, tenant_id: uuid.UUID, month: str, *, supplier_gstin: str | None = None
    ) -> PeriodReport:
        """The month's report: what the preview shows and the file holds. Raises
        `RegisterInvalid` for a bad month or GSTIN."""
        first, last = month_bounds(month)
        supplier = None
        if supplier_gstin and supplier_gstin.strip():
            supplier = gstin_key(supplier_gstin)
            if supplier is None or not is_valid_gstin(supplier):
                raise RegisterInvalid(
                    f"'{supplier_gstin.strip()}' is not a valid GSTIN: check its 15 characters."
                )
        with self._sessions() as session:
            tolerances = variance_tolerances(session, tenant_id)
            signed, unsigned = _invoices(session, tenant_id, first, last)
            if supplier is not None:
                signed = [s for s in signed if gstin_key(s[3].seller.gstin.value) == supplier]
                unsigned = [u for u in unsigned if gstin_key(u[4]) == supplier]
            pairs = _orders(session, tenant_id, [s[1] for s in signed if s[2] == "approved"])
            orders = _order_records(session, tenant_id, {pair[1] for pair in pairs.values()})
            names = _book_names(
                session,
                tenant_id,
                [s[3].seller.gstin.value for s in signed] + [u[4] for u in unsigned],
            )
        checked: list[Checked] = []
        not_checked: list[NotChecked] = []

        def named(gstin: str | None, printed: str | None) -> str | None:
            key = gstin_key(gstin)
            return (names.get(key) if key else None) or printed

        for document_id, version_id, outcome, invoice in signed:
            gstin, printed = invoice.seller.gstin.value, invoice.seller.name.value
            pair = pairs.get(version_id)
            purchase = orders.get(pair[1]) if pair is not None else None
            if outcome != "approved":
                reason: str | None = REJECTED
            elif pair is None:
                reason = NO_ORDER
            elif purchase is None:
                reason = ORDER_UNREADABLE
            else:
                reason = None
            if pair is None or purchase is None or reason is not None:
                not_checked.append(
                    NotChecked(
                        document_id, invoice.invoice_no.value, invoice.invoice_date.value,
                        gstin_key(gstin) or gstin, named(gstin, printed), reason or NO_ORDER,
                    )
                )  # fmt: skip
                continue
            checked.append(
                Checked(
                    document_id, pair[0], invoice.invoice_date.value, named(gstin, printed),
                    compare(invoice, purchase, tolerances),
                )
            )  # fmt: skip
        for document_id, number, day, printed, gstin in unsigned:
            not_checked.append(
                NotChecked(document_id, number, day, gstin_key(gstin) or gstin,
                           named(gstin, printed), NOT_APPROVED)
            )  # fmt: skip
        checked.sort(key=lambda c: (c.invoice_date or first, c.variance.invoice_no or ""))
        not_checked.sort(key=lambda n: (n.invoice_date or first, n.invoice_no or ""))
        return PeriodReport(
            month=first,
            supplier_gstin=supplier,
            tolerances=tolerances,
            checked=tuple(checked),
            not_checked=tuple(not_checked),
            suppliers=_by_supplier(checked),
            totals=SupplierSummary.of(None, None, checked),
        )

    @scoped
    def workbook(
        self,
        tenant_id: uuid.UUID,
        month: str,
        *,
        supplier_gstin: str | None = None,
        actor: str,
        person: str,
    ) -> tuple[str, bytes, PeriodReport]:
        """The report as an Excel file (and its name). Raises `RegisterInvalid` or, with no
        invoice dated in the month at all, `VarianceEmpty`."""
        report = self.build(tenant_id, month, supplier_gstin=supplier_gstin)
        if not report.checked and not report.not_checked:
            who = f" from {report.supplier_gstin}" if report.supplier_gstin else ""
            raise VarianceEmpty(
                f"No invoice{who} is dated in {month_name(report.month)}, so there is nothing "
                "to compare: choose another month."
            )
        with self._sessions() as session:
            tenant = session.get(Tenant, tenant_id)
            workspace = (tenant.display_name or tenant.name) if tenant else str(tenant_id)
        data = write_workbook(report, workspace=workspace, person=person)
        sha256 = hashlib.sha256(data).hexdigest()
        label = report.month.strftime("%Y-%m")
        with self._sessions.begin() as session:
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="variance.exported",
                target_type="po_variance_report", target_id=label,
                details={
                    "month": label, "invoices_checked": len(report.checked),
                    "not_checked": len(report.not_checked),
                    "variances": report.totals.variances,
                    "supplier_filter": report.supplier_gstin is not None, "sha256": sha256,
                },
            )  # fmt: skip
        supplier = f"-{report.supplier_gstin}" if report.supplier_gstin else ""
        return f"docforge-po-variance-{label}{supplier}.xlsx", data, report


# A page with no blocks: normalising with it gives the values as read and corrected, without
# the citations (which the variance does not read) and without loading the parse.
_NO_BLOCKS = ParsedDocument(
    parser="none", parser_version="0", pages=(Page(number=1, width=1, height=1),), blocks=()
)


def _order_records(
    session: Session, tenant_id: uuid.UUID, versions: set[uuid.UUID]
) -> dict[uuid.UUID, PurchaseOrderExtraction]:
    """Each order version's values as corrected, the review's own way (its reading with its
    corrections applied in order, normalised), in three queries for all of them: its signed
    record when it was signed (nothing changes after a signature), else its reading."""
    found: dict[uuid.UUID, PurchaseOrderExtraction] = {}
    if not versions:
        return found
    ids = list(versions)
    for version_id, data in session.execute(
        select(Review.document_version_id, Review.data["record"]).where(
            Review.tenant_id == tenant_id, Review.document_version_id.in_(ids)
        )
    ):
        if (record := _record(PurchaseOrderExtraction, data)) is not None:
            found[version_id] = record
    rest = [v for v in ids if v not in found]
    if not rest:
        return found
    corrections: dict[uuid.UUID, list[Corrected]] = {}
    for version_id, path, text in session.execute(
        select(Correction.document_version_id, Correction.path, Correction.new_text)
        .where(Correction.tenant_id == tenant_id, Correction.document_version_id.in_(rest))
        .order_by(Correction.created_at, Correction.id)
    ):
        corrections.setdefault(version_id, []).append(Corrected(path=path, text=text))
    for version_id, raw, data in session.execute(
        select(Extraction.document_version_id, Extraction.raw, Extraction.data).where(
            Extraction.tenant_id == tenant_id, Extraction.document_version_id.in_(rest)
        )
    ):
        try:
            if raw is None:  # read before replies were kept: as extracted
                found[version_id] = PurchaseOrderExtraction.model_validate(data)
                continue
            corrected = apply_corrections(
                RawPurchaseOrder.model_validate(raw), corrections.get(version_id, ())
            )
            found[version_id] = normalize_purchase_order(corrected, _NO_BLOCKS)
        except (ValidationError, ValueError):
            continue  # not readable: the invoice is listed as not checked
    return found


def _by_supplier(checked: Sequence[Checked]) -> tuple[SupplierSummary, ...]:
    groups: dict[str, list[Checked]] = {}
    for row in checked:
        key = gstin_key(row.variance.supplier_gstin) or (row.supplier_name or "").casefold()
        groups.setdefault(key, []).append(row)
    found = [
        SupplierSummary.of(gstin_key(rows[0].variance.supplier_gstin), rows[0].supplier_name, rows)
        for rows in groups.values()
    ]
    return tuple(sorted(found, key=lambda s: (-s.above_po, s.supplier_name or "")))


def _newest() -> Any:
    newer = aliased(DocumentVersion)
    return (
        select(func.max(newer.version_no))
        .where(newer.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )


def _invoices(
    session: Session, tenant_id: uuid.UUID, first: date, last: date
) -> tuple[list[Any], list[Any]]:
    """Live invoices dated in the month, by their newest read version.

    Signed: (document, version, outcome, the signed record), dated by the record. Not signed:
    (document, invoice number, date, printed seller, GSTIN), dated as read or as corrected."""
    day = Review.data[("record", "invoice_date", "value")].astext
    signed_rows = session.execute(
        select(Document.id, DocumentVersion.id, Review.outcome, Review.data["record"])
        .join(DocumentVersion, DocumentVersion.id == Review.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Review.tenant_id == tenant_id,
            Document.tenant_id == tenant_id,
            Document.doc_type == "invoice",
            live(),
            DocumentVersion.version_no == _newest(),
            day >= first.isoformat(),  # ISO dates compare as text
            day <= last.isoformat(),
        )
        .limit(MAX_INVOICES + 1)
    ).all()
    if len(signed_rows) > MAX_INVOICES:
        raise RegisterInvalid(
            f"More than {MAX_INVOICES:,} invoices are dated in that month: choose one supplier "
            "at a time."
        )
    signed = [
        (document_id, version_id, outcome, record)
        for document_id, version_id, outcome, data in signed_rows
        if (record := _record(InvoiceExtraction, data)) is not None
    ]

    def at(*keys: str) -> Any:
        return Extraction.data[(*keys, "value")].astext

    reviewed = select(Review.id).where(Review.document_version_id == DocumentVersion.id)
    read = session.execute(
        select(Document.id, DocumentVersion.id, at("invoice_no"), at("invoice_date"),
               at("seller", "name"), at("seller", "gstin"))
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Extraction, Extraction.document_version_id == DocumentVersion.id)
        .where(
            Document.tenant_id == tenant_id,
            Extraction.tenant_id == tenant_id,
            Document.doc_type == "invoice",
            live(),
            DocumentVersion.version_no == _newest(),
            ~reviewed.exists(),
        )
    ).all()  # fmt: skip
    corrected: dict[tuple[uuid.UUID, str], str | None] = {}
    if read:
        for version_id, path, text in session.execute(
            select(Correction.document_version_id, Correction.path, Correction.new_text)
            .where(
                Correction.tenant_id == tenant_id,
                Correction.document_version_id.in_([row[1] for row in read]),
                Correction.path.in_(("invoice_no", "invoice_date", "seller.name", "seller.gstin")),
            )
            .order_by(Correction.created_at, Correction.id)
        ):
            corrected[(version_id, path)] = text
    unsigned = []
    for document_id, version_id, number, read_day, name, gstin in read:

        def value(path: str, as_read: Any, version: uuid.UUID = version_id) -> Any:
            return corrected.get((version, path), as_read)

        text_day = value("invoice_date", None)
        when = parse_date(text_day) if text_day else _iso(read_day)
        if when is not None and first <= when <= last:
            unsigned.append((document_id, value("invoice_no", number), when,
                             value("seller.name", name), value("seller.gstin", gstin)))  # fmt: skip
    return signed, unsigned


def _iso(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _orders(
    session: Session, tenant_id: uuid.UUID, invoice_versions: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]]:
    """Each invoice version's current pair, as `documents.current_match` finds it one at a
    time: the newest comparison whose order is still a live document's newest version. Invoice
    version -> (order document, order version)."""
    if not invoice_versions:
        return {}
    other = aliased(DocumentVersion)
    newer = aliased(DocumentVersion)
    found: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
    for start in range(0, len(invoice_versions), 5_000):
        chunk = list(invoice_versions[start : start + 5_000])
        rows = session.execute(
            select(MatchRecord.invoice_version_id, other.document_id, other.id)
            .join(other, other.id == MatchRecord.order_version_id)
            .join(Document, Document.id == other.document_id)
            .where(
                MatchRecord.tenant_id == tenant_id,
                MatchRecord.invoice_version_id.in_(chunk),
                Document.tenant_id == tenant_id,
                live(),
                other.version_no
                == select(func.max(newer.version_no))
                .where(newer.document_id == other.document_id)
                .scalar_subquery(),
            )
            .order_by(MatchRecord.invoice_version_id, MatchRecord.created_at.desc())
        )
        for invoice_version, order_document, order_version in rows:
            found.setdefault(invoice_version, (order_document, order_version))
    return found


def _book_names(
    session: Session, tenant_id: uuid.UUID, gstins: Iterable[str | None]
) -> dict[str, str]:
    """The name the workspace's books give each supplier (its mapping, V2 P1)."""
    keys = {key for gstin in gstins if (key := gstin_key(gstin))}
    if not keys:
        return {}
    mappings = session.scalars(
        select(SupplierMapping).where(
            SupplierMapping.tenant_id == tenant_id, SupplierMapping.seller_gstin.in_(keys)
        )
    ).all()
    return {m.seller_gstin: name for m in mappings if (name := m.tally_ledger or m.marg_party)}


# The workbook

SUMMARY_HEADERS = (
    "Supplier GSTIN", "Supplier", "Invoices checked", "Lines checked", "Lines matched",
    "Variances found", "Not compared", "Lines not in PO", "PO lines not billed",
    "Charged above PO (INR)", "Net money impact (INR)",
)  # fmt: skip
INVOICE_HEADERS = (
    "Invoice Number", "Invoice Date", "PO Number", "Supplier GSTIN", "Supplier",
    "Invoice Total", "PO Total (matched lines)", "Net Variance (INR)", "Net Variance (%)",
    "Lines checked", "Lines matched", "Variances found", "Lines not in PO",
    "PO lines not billed",
)  # fmt: skip
VARIANCE_HEADERS = (
    "Invoice Number", "PO Number", "Supplier", "Bill line", "Product", "Field", "Status",
    "PO value", "Billed value", "Difference", "Money impact (INR)", "Working", "Note",
)  # fmt: skip
UNPAIRED_HEADERS = (
    "Section", "Invoice Number", "PO Number", "Supplier", "Line", "Product", "Qty", "Rate",
    "Value (INR)", "Working", "Note",
)  # fmt: skip
NOT_CHECKED_HEADERS = ("Invoice Number", "Invoice Date", "Supplier GSTIN", "Supplier", "Reason")
_STATUS_WORDS = {
    "variance": "Variance",
    "within_tolerance": "Matched (within tolerance)",
    "not_compared": "Not compared",
}


def write_workbook(
    report: PeriodReport, *, workspace: str, person: str, now: datetime | None = None
) -> bytes:
    """Streamed (openpyxl's write-only mode): money and quantities as numbers, dates as dates,
    every text as text - never a formula."""
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

    def rupees(sheet: Any, value: Decimal | None, *, strong: bool = False) -> Any:
        cell = WriteOnlyCell(sheet, value=None if value is None else money(value))
        cell.number_format = _MONEY
        if strong:
            cell.font = bold
        return cell

    def number(sheet: Any, value: Decimal | int | None, *, strong: bool = False) -> Any:
        cell = WriteOnlyCell(sheet, value=value)
        if strong:
            cell.font = bold
        return cell

    def percent(sheet: Any, value: Decimal | None) -> Any:
        cell = WriteOnlyCell(sheet, value=value)
        cell.number_format = _PERCENT
        return cell

    def day(sheet: Any, value: date | None) -> Any:
        cell = WriteOnlyCell(sheet, value=value)
        cell.number_format = _DATE
        return cell

    def header(sheet: Any, names: Sequence[str]) -> None:
        sheet.append([text(sheet, name, strong=True) for name in names])

    def summary_row(sheet: Any, s: SupplierSummary, *, strong: bool = False) -> list[Any]:
        counts = (s.invoices_checked, s.lines_checked, s.lines_matched, s.variances,
                  s.not_compared, s.not_in_po_lines, s.not_billed_lines)  # fmt: skip
        return [
            text(sheet, "Total" if strong else s.supplier_gstin, strong=strong),
            text(sheet, None if strong else s.supplier_name),
            *(number(sheet, n, strong=strong) for n in counts),
            rupees(sheet, s.above_po, strong=strong),
            rupees(sheet, s.net_impact, strong=strong),
        ]

    sheet = book.create_sheet("Summary")
    header(sheet, SUMMARY_HEADERS)
    for s in report.suppliers:
        sheet.append(summary_row(sheet, s))
    sheet.append(summary_row(sheet, report.totals, strong=True))
    sheet.append([])
    count = len(report.not_checked)
    plural = "" if count == 1 else "s"
    sheet.append([text(sheet, f"Not checked: {count} invoice{plural} (see 'Not checked')")])

    invoices = book.create_sheet("Invoices")
    header(invoices, INVOICE_HEADERS)
    for c in report.checked:
        v = c.variance
        invoices.append([
            text(invoices, v.invoice_no), day(invoices, c.invoice_date), text(invoices, v.po_no),
            text(invoices, gstin_key(v.supplier_gstin) or v.supplier_gstin),
            text(invoices, c.supplier_name), rupees(invoices, v.invoice_total),
            rupees(invoices, v.po_total), rupees(invoices, v.net_impact),
            percent(invoices, v.net_percent), number(invoices, len(v.lines)),
            number(invoices, v.matched_lines), number(invoices, v.variances),
            number(invoices, len(v.not_in_po)), number(invoices, len(v.not_billed)),
        ])  # fmt: skip

    rows = book.create_sheet("Variances")
    header(rows, VARIANCE_HEADERS)
    for c in report.checked:
        v = c.variance
        for line in v.lines:
            for d in line.differences:
                note = "; ".join(n for n in (d.note, line.note) if n) or None
                rows.append([
                    text(rows, v.invoice_no), text(rows, v.po_no), text(rows, c.supplier_name),
                    number(rows, line.invoice_line + 1), text(rows, line.product),
                    text(rows, LABELS[d.field]), text(rows, _STATUS_WORDS[d.status]),
                    number(rows, d.po_value), number(rows, d.billed_value),
                    number(rows, d.difference), rupees(rows, d.money_impact),
                    text(rows, d.working), text(rows, note),
                ])  # fmt: skip

    apart = book.create_sheet("Not in PO - Not billed")
    header(apart, UNPAIRED_HEADERS)
    for c in report.checked:
        v = c.variance
        for section, found in (("Not in PO", v.not_in_po), ("Not billed", v.not_billed)):
            for u in found:
                apart.append([
                    text(apart, section), text(apart, v.invoice_no), text(apart, v.po_no),
                    text(apart, c.supplier_name), number(apart, u.line + 1),
                    text(apart, u.product), number(apart, u.qty), number(apart, u.rate),
                    rupees(apart, u.value), text(apart, u.working), text(apart, u.note),
                ])  # fmt: skip

    unchecked = book.create_sheet("Not checked")
    header(unchecked, NOT_CHECKED_HEADERS)
    for n in report.not_checked:
        unchecked.append([
            text(unchecked, n.invoice_no), day(unchecked, n.invoice_date),
            text(unchecked, n.supplier_gstin), text(unchecked, n.supplier_name),
            text(unchecked, n.reason),
        ])  # fmt: skip

    about = book.create_sheet("About")
    made = (now or datetime.now(_INDIA)).astimezone(_INDIA)
    t = report.tolerances
    tolerances = (
        f"Rate {t.rate} INR per unit; Qty {t.qty}; Free Qty {t.free_qty}; "
        f"Taxable Amount {t.amount} INR"
    )
    for label, value in (
        ("Month", month_name(report.month)),
        ("Workspace", workspace),
        ("Generated at", made.strftime("%d %b %Y, %H:%M IST")),
        ("Generated by", person),
        ("Supplier", report.supplier_gstin or "All suppliers"),
        ("Invoices checked", str(len(report.checked))),
        ("Invoices not checked", str(len(report.not_checked))),
        ("Tolerances", tolerances),
    ):
        about.append([text(about, label, strong=True), text(about, value)])
    about.append([])
    for sentence in (ABOUT, SCOPE, SIGN_NOTE):
        about.append([text(about, sentence)])

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
