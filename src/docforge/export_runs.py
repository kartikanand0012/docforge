"""Export runs: the workspace's approved bills, booked under its own Tally / Marg names (V2 P1).

A run takes the invoices approved in a range of India's dates. It never guesses a name: if
any bill's supplier or item has no mapping for the books it is exported to, or a bill lacks
what booking needs (a valid seller GSTIN, a product name), the run stores that task list and
makes no file. Otherwise it writes one CSV row per bill line, through `csvsafe`, stores the
file and keeps its SHA-256; a download returns those bytes, checked against the hash, so a
mapping changed later reaches later runs only.

Runs are immutable rows (the application may insert and read them, never change them), and
each is audited - created and downloaded - with counts and the hash, never names.

The CSV is the mapped purchase register Tally and Marg imports (V2 P2, P3) build on. Values are
the signed record's: amounts as their exact decimals, dates as YYYY-MM-DD, expiry as YYYY-MM.
"""

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from docforge import audit
from docforge.csvsafe import to_csv
from docforge.db.models import Document, DocumentVersion, ExportRun, Review, live
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.extraction.schema import InvoiceExtraction
from docforge.mappings.keys import gstin_key, item_key
from docforge.mappings.service import Bill, BillProblem, MappingService, Resolution, Tasks, resolve
from docforge.storage import ObjectStore

Target = Literal["tally", "marg"]
Status = Literal["needs_mapping", "ready"]

_INDIA = ZoneInfo("Asia/Kolkata")
MAX_RANGE_DAYS = 366
MAX_RUN_INVOICES = 2_000  # a month of a busy distributor's purchases, many times over
MAX_LIST = 100
COLUMNS = (
    "voucher_date", "supplier_invoice_no", "supplier_gstin", "party", "purchase_ledger",
    "tax_treatment", "place_of_supply_code", "line_no", "item", "printed_product_name", "pack",
    "hsn", "batch_no", "expiry", "qty", "free_qty", "rate", "mrp", "discount_pct",
    "taxable_value", "gst_rate", "amount", "invoice_taxable_value", "cgst", "sgst", "igst",
    "round_off", "invoice_total", "document_id",
)  # fmt: skip


class RunRangeInvalid(ValueError):
    """The range ends before it starts or is longer than `MAX_RANGE_DAYS`."""


class TooManyInvoices(ValueError):
    """More than `MAX_RUN_INVOICES` bills were approved in the range."""


class RunNotFound(LookupError):
    """No such run in this workspace."""


class NoFile(LookupError):
    """The run met something unmapped, so it made no file."""


@dataclass(frozen=True)
class RunView:
    id: uuid.UUID
    target: Target
    start: date
    end: date
    status: Status
    invoices: int
    lines: int
    tasks: Tasks | None  # what is still to map; None when the run made its file
    filename: str | None
    sha256: str | None
    size_bytes: int | None
    created_by: str
    created_at: datetime


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, date) else str(value)


def _rows(bills: Sequence[Bill], resolution: Resolution, target: Target) -> list[dict[str, Any]]:
    rows = []
    for bill in bills:
        invoice = bill.invoice
        gstin = gstin_key(invoice.seller.gstin.value) or ""
        supplier = resolution.suppliers[gstin]
        totals = invoice.totals
        for number, line in enumerate(invoice.lines, start=1):
            item = resolution.items[item_key(line.product_name.value, line.pack.value) or ""]
            rows.append(
                {
                    "voucher_date": _text(invoice.invoice_date.value),
                    "supplier_invoice_no": invoice.invoice_no.value,
                    "supplier_gstin": gstin,
                    "party": supplier.name_for(target),
                    "purchase_ledger": supplier.purchase_ledger,
                    "tax_treatment": supplier.tax_treatment,
                    "place_of_supply_code": invoice.place_of_supply_code.value,
                    "line_no": number,
                    "item": item.name_for(target),
                    "printed_product_name": line.product_name.value,
                    "pack": line.pack.value,
                    "hsn": item.preferred_hsn or line.hsn.value,
                    "batch_no": line.batch_no.value,
                    "expiry": line.expiry.value,
                    "qty": _text(line.qty.value),
                    "free_qty": _text(line.free_qty.value),
                    "rate": _text(line.ptr.value),
                    "mrp": _text(line.mrp.value),
                    "discount_pct": _text(line.discount_pct.value),
                    "taxable_value": _text(line.taxable_value.value),
                    "gst_rate": _text(line.gst_rate.value),
                    "amount": _text(line.amount.value),
                    "invoice_taxable_value": _text(totals.taxable_value.value),
                    "cgst": _text(totals.cgst.value),
                    "sgst": _text(totals.sgst.value),
                    "igst": _text(totals.igst.value),
                    "round_off": _text(totals.round_off.value),
                    "invoice_total": _text(totals.grand_total.value),
                    "document_id": str(bill.document_id),
                }
            )
    return rows


def _approved(
    session: Session, tenant_id: uuid.UUID, lower: datetime, upper: datetime
) -> tuple[list[Bill], list[BillProblem]]:
    """Live invoices whose newest version was approved from `lower` to before `upper`, in the
    order they were approved."""
    newer = aliased(DocumentVersion)
    newest = (
        select(func.max(newer.version_no))
        .where(newer.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )
    rows = session.execute(
        select(Review.data, Document.id, Document.filename)
        .join(DocumentVersion, DocumentVersion.id == Review.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Review.tenant_id == tenant_id,
            Document.tenant_id == tenant_id,
            Review.outcome == "approved",
            Review.signed_at >= lower,
            Review.signed_at < upper,
            Document.doc_type == "invoice",
            live(),
            DocumentVersion.version_no == newest,
        )
        .order_by(Review.signed_at, Document.id)
        .limit(MAX_RUN_INVOICES + 1)
    ).all()
    if len(rows) > MAX_RUN_INVOICES:
        raise TooManyInvoices(
            f"More than {MAX_RUN_INVOICES:,} bills were approved in that range. Choose a "
            "shorter range."
        )
    bills, problems = [], []
    for data, document_id, filename in rows:
        try:
            invoice = InvoiceExtraction.model_validate((data or {}).get("record") or {})
        except ValidationError:
            problems.append(
                BillProblem(
                    document_id,
                    filename,
                    "This bill's signed record cannot be read for export: ask for help.",
                )
            )
            continue
        bills.append(Bill(document_id, filename, invoice))
    return bills, problems


class ExportRunService:
    def __init__(
        self, sessions: SessionFactory, store: ObjectStore, mappings: MappingService
    ) -> None:
        self._sessions = sessions
        self._store = store
        self.mappings = mappings  # whose master the runs read; for suggestions on a run

    @scoped
    def create(
        self,
        tenant_id: uuid.UUID,
        *,
        target: str,
        start: date,
        end: date,
        actor: str,
        name: str,
    ) -> RunView:
        """Export the invoices approved from `start` to `end` (India's dates, both included)
        for `target`'s books. Raises `RunRangeInvalid` or `TooManyInvoices`."""
        if target not in ("tally", "marg"):
            raise ValueError("an export is for tally or marg")
        books: Target = "tally" if target == "tally" else "marg"
        if end < start:
            raise RunRangeInvalid("the range ends before it starts")
        if (end - start).days >= MAX_RANGE_DAYS:
            raise RunRangeInvalid(f"a run covers at most {MAX_RANGE_DAYS} days")
        lower = datetime.combine(start, time(), _INDIA)
        upper = datetime.combine(end + timedelta(days=1), time(), _INDIA)
        run_id = uuid.uuid4()
        with self._sessions() as session:
            bills, unreadable = _approved(session, tenant_id, lower, upper)
            resolution = resolve(session, tenant_id, bills, books)
        found = resolution.tasks
        tasks = Tasks(found.suppliers, found.items, (*unreadable, *found.problems))
        lines = sum(len(bill.invoice.lines) for bill in bills)
        file_key = filename = sha256 = None
        size = None
        if not tasks:
            data = to_csv(_rows(bills, resolution, books), fieldnames=COLUMNS).encode()
            sha256 = hashlib.sha256(data).hexdigest()
            size = len(data)
            filename = f"docforge-{books}-{start}-to-{end}-{str(run_id)[:8]}.csv"
            file_key = f"exports/{tenant_id}/{run_id}.csv"
            self._store.put(file_key, data, "text/csv; charset=utf-8")
        status: Status = "needs_mapping" if tasks else "ready"
        invoices = len(bills) + len(unreadable)
        with self._sessions.begin() as session:
            row = ExportRun(
                id=run_id, tenant_id=tenant_id, target=books, range_start=start, range_end=end,
                status=status, invoices=invoices, lines=lines,
                tasks=tasks.to_json() if tasks else None, file_key=file_key, filename=filename,
                sha256=sha256, size_bytes=size, created_by=name[:200],
            )  # fmt: skip
            session.add(row)
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="export.created",
                target_type="export_run", target_id=str(run_id),
                details={
                    "target": books, "status": status, "invoices": invoices, "lines": lines,
                    "unmapped_suppliers": len(tasks.suppliers),
                    "unmapped_items": len(tasks.items), "problems": len(tasks.problems),
                    "sha256": sha256,
                },
            )  # fmt: skip
            session.flush()
            session.refresh(row)
            return _view(row)

    @scoped
    def list(self, tenant_id: uuid.UUID, *, limit: int = 50) -> list[RunView]:
        """Newest first."""
        with self._sessions() as session:
            rows = session.scalars(
                select(ExportRun)
                .where(ExportRun.tenant_id == tenant_id)
                .order_by(ExportRun.created_at.desc(), ExportRun.id.desc())
                .limit(min(max(limit, 1), MAX_LIST))
            )
            return [_view(row) for row in rows]

    @scoped
    def get(self, tenant_id: uuid.UUID, run_id: uuid.UUID) -> RunView:
        """Raises `RunNotFound`."""
        with self._sessions() as session:
            return _view(_find(session, tenant_id, run_id))

    @scoped
    def file(self, tenant_id: uuid.UUID, run_id: uuid.UUID, *, actor: str) -> tuple[str, bytes]:
        """The run's file as it was made, and its name. Raises `RunNotFound` or `NoFile`."""
        with self._sessions() as session:
            row = _find(session, tenant_id, run_id)
            if row.file_key is None or row.filename is None or row.sha256 is None:
                raise NoFile(run_id)
            key, filename, sha256 = row.file_key, row.filename, row.sha256
        data = self._store.get(key)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise RuntimeError(f"export run {run_id}'s stored file is not the file it made")
        with self._sessions.begin() as session:
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="export.downloaded",
                target_type="export_run", target_id=str(run_id), details={"sha256": sha256},
            )  # fmt: skip
        return filename, data


def _find(session: Session, tenant_id: uuid.UUID, run_id: uuid.UUID) -> ExportRun:
    row = session.scalars(
        select(ExportRun).where(ExportRun.tenant_id == tenant_id, ExportRun.id == run_id)
    ).one_or_none()
    if row is None:
        raise RunNotFound(run_id)
    return row


def _view(row: ExportRun) -> RunView:
    return RunView(
        id=row.id,
        target="tally" if row.target == "tally" else "marg",
        start=row.range_start,
        end=row.range_end,
        status="ready" if row.status == "ready" else "needs_mapping",
        invoices=row.invoices,
        lines=row.lines,
        tasks=Tasks.from_json(row.tasks) if row.tasks is not None else None,
        filename=row.filename,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        created_by=row.created_by,
        created_at=row.created_at,
    )
