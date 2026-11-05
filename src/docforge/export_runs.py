"""Export runs: the workspace's approved bills, booked under its own Tally / Marg names (V2 P1),
and as a Marg purchase import in the workspace's own layout (V2 P3).

A run takes the invoices approved in a range of India's dates. It never guesses a name: if
any bill's supplier or item has no mapping for the books it is exported to, or a bill lacks
what booking needs (a valid seller GSTIN, a product name), the run stores that task list and
makes no file. Otherwise it writes the file, stores it and keeps its SHA-256; a download
returns those bytes, checked against the hash, so a later change reaches later runs only.

Formats:

- `mapped_csv` (P1): one CSV row per bill line under the mapped names, through `csvsafe`.
  Amounts as their exact decimals, dates YYYY-MM-DD, expiry YYYY-MM.
- `marg_import` (P3): the workspace's active Marg profile (`docforge.profiles`) - its
  layout, made from an import file its Marg accepted. With no active profile there is no
  run, and the refusal says what is needed. Before any file, every bill passes pre-flight
  (`docforge.preflight`, shared with the Tally XML): totals reconciled, tax split, batch and
  expiry on each medicine line. A run names the profile version it used.

A run warns, without refusing, when some of its bills were in an earlier run's file for the
same books ("12 of these bills were in the run of 12 Oct").

Runs are immutable rows (the application may insert and read them, never change them), and
each is audited - created and downloaded - with counts and the hash, never names.
"""

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session, aliased

from docforge import audit
from docforge.csvsafe import to_csv
from docforge.db.models import (
    Document,
    DocumentVersion,
    ExportRun,
    ExportRunDocument,
    Review,
    live,
)
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.extraction.schema import InvoiceExtraction, LineExtraction
from docforge.mappings.keys import gstin_key, item_key
from docforge.mappings.service import Bill, BillProblem, MappingService, Resolution, Tasks, resolve
from docforge.preflight import preflight
from docforge.profiles.compare import Comparison, compare
from docforge.profiles.render import Booked, render, table, unwritable
from docforge.profiles.service import ProfileService, VersionView
from docforge.storage import ObjectStore

Target = Literal["tally", "marg"]
Format = Literal["mapped_csv", "marg_import"]
Status = Literal["needs_mapping", "ready"]

_INDIA = ZoneInfo("Asia/Kolkata")
MAX_RANGE_DAYS = 366
MAX_RUN_INVOICES = 2_000  # a month of a busy distributor's purchases, many times over
MAX_LIST = 100
MAX_CHECK_BILLS = 50
MAX_OVERLAPS = 10
COLUMNS = (
    "voucher_date", "supplier_invoice_no", "supplier_gstin", "party", "purchase_ledger",
    "tax_treatment", "place_of_supply_code", "line_no", "item", "printed_product_name", "pack",
    "hsn", "batch_no", "expiry", "qty", "free_qty", "rate", "mrp", "discount_pct",
    "taxable_value", "gst_rate", "amount", "invoice_taxable_value", "cgst", "sgst", "igst",
    "round_off", "invoice_total", "document_id",
)  # fmt: skip
_MEDIA = {"csv": "text/csv; charset=utf-8",
          "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}  # fmt: skip


class RunRangeInvalid(ValueError):
    """The range ends before it starts or is longer than `MAX_RANGE_DAYS`."""


class TooManyInvoices(ValueError):
    """More than `MAX_RUN_INVOICES` bills were approved in the range."""


class RunNotFound(LookupError):
    """No such run in this workspace."""


class NoFile(LookupError):
    """The run met something unmapped, so it made no file."""


class CheckInvalid(ValueError):
    """A check against an accepted file cannot be made, said in words."""


@dataclass(frozen=True)
class ProfileRef:
    """The profile version a Marg import was made in."""

    id: uuid.UUID  # the version's
    profile_id: uuid.UUID
    name: str
    version_no: int
    basis: str  # sample (made-up), upload or manual


@dataclass(frozen=True)
class Overlap:
    """An earlier run whose file booked some of this run's bills."""

    run_id: uuid.UUID
    created_at: datetime
    format: Format
    invoices: int


@dataclass(frozen=True)
class RunView:
    id: uuid.UUID
    target: Target
    format: Format
    start: date
    end: date
    status: Status
    invoices: int
    lines: int
    tasks: Tasks | None  # what is still to map or correct; None when the run made its file
    filename: str | None
    sha256: str | None
    size_bytes: int | None
    profile: ProfileRef | None
    overlaps: tuple[Overlap, ...]
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class CheckView:
    """How the profile's file for some bills compares with the file the books accepted."""

    version: ProfileRef
    bills: int
    tasks: Tasks | None  # what must be mapped or corrected before the bills can be compared
    comparison: Comparison | None


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


def _booked(bills: Sequence[Bill], resolution: Resolution) -> list[Booked]:
    """Each bill with its party and items as the books name them (all mapped)."""
    return [
        Booked(
            bill,
            resolution.suppliers[gstin_key(bill.invoice.seller.gstin.value) or ""],
            tuple(
                resolution.items[item_key(line.product_name.value, line.pack.value) or ""]
                for line in bill.invoice.lines
            ),
        )
        for bill in bills
    ]


def _approved(
    session: Session,
    tenant_id: uuid.UUID,
    lower: datetime | None = None,
    upper: datetime | None = None,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> tuple[list[Bill], list[BillProblem]]:
    """Live invoices whose newest version was approved from `lower` to before `upper` (or
    those of `document_ids` that are), in the order they were approved."""
    newer = aliased(DocumentVersion)
    newest = (
        select(func.max(newer.version_no))
        .where(newer.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )
    query = (
        select(Review.data, Document.id, Document.filename)
        .join(DocumentVersion, DocumentVersion.id == Review.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Review.tenant_id == tenant_id,
            Document.tenant_id == tenant_id,
            Review.outcome == "approved",
            Document.doc_type == "invoice",
            live(),
            DocumentVersion.version_no == newest,
        )
    )
    if lower is not None and upper is not None:
        query = query.where(Review.signed_at >= lower, Review.signed_at < upper)
    if document_ids is not None:
        query = query.where(Document.id.in_(list(document_ids)))
    rows = session.execute(
        query.order_by(Review.signed_at, Document.id).limit(MAX_RUN_INVOICES + 1)
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


def _overlaps(
    session: Session, tenant_id: uuid.UUID, target: Target, document_ids: Sequence[uuid.UUID]
) -> tuple[list[Overlap], int]:
    """Earlier runs for the same books whose files booked any of these bills (newest
    first), and how many of the bills any of them booked."""
    if not document_ids:
        return [], 0
    ids = list(document_ids)
    earlier = (
        select(ExportRunDocument.document_id, ExportRun.id, ExportRun.created_at, ExportRun.format)
        .join(ExportRun, ExportRun.id == ExportRunDocument.run_id)
        .where(
            ExportRunDocument.tenant_id == tenant_id,
            ExportRun.tenant_id == tenant_id,
            ExportRun.target == target,
            ExportRunDocument.document_id.in_(ids),
        )
    ).subquery()
    found = session.execute(
        select(earlier.c.id, earlier.c.created_at, earlier.c.format, func.count())
        .group_by(earlier.c.id, earlier.c.created_at, earlier.c.format)
        .order_by(earlier.c.created_at.desc(), earlier.c.id.desc())
        .limit(MAX_OVERLAPS)
    ).all()
    before = session.scalar(select(func.count(func.distinct(earlier.c.document_id)))) or 0
    return [Overlap(run, at, _format(kind), count) for run, at, kind, count in found], before


def _unwritable(
    version: VersionView, bills: Sequence[Bill], resolution: Resolution
) -> tuple[BillProblem, ...]:
    """A value an Excel file cannot hold (a control character the reading left, or text
    too long for a cell), named by bill, line and column, before any file is made."""
    return tuple(
        BillProblem(
            cell.booked.bill.document_id,
            cell.booked.bill.filename,
            f"{'Line ' + str(cell.line) + ': ' if cell.line else ''}the {cell.header} value "
            "has a character or a length an Excel file cannot hold: correct it on the bill, "
            "then export again.",
            cell.line,
        )
        for cell in unwritable(version.spec, _booked(bills, resolution))
    )


def _format(value: str) -> Format:
    return "marg_import" if value == "marg_import" else "mapped_csv"


def _ref(version: VersionView) -> ProfileRef:
    return ProfileRef(
        version.id, version.profile_id, version.profile_name, version.version_no, version.basis
    )


class ExportRunService:
    def __init__(
        self,
        sessions: SessionFactory,
        store: ObjectStore,
        mappings: MappingService,
        profiles: ProfileService | None = None,
    ) -> None:
        self._sessions = sessions
        self._store = store
        self.mappings = mappings  # whose master the runs read; for suggestions on a run
        self.profiles = profiles or ProfileService(sessions)

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
        file_format: str = "mapped_csv",
    ) -> RunView:
        """Export the invoices approved from `start` to `end` (India's dates, both included)
        for `target`'s books, in `file_format`. Raises `RunRangeInvalid`, `TooManyInvoices`,
        or for a Marg import with no active profile `NoActiveProfile`."""
        if target not in ("tally", "marg"):
            raise ValueError("an export is for tally or marg")
        if file_format not in ("mapped_csv", "marg_import"):
            raise ValueError("an export is a mapped_csv or a marg_import")
        books: Target = "tally" if target == "tally" else "marg"
        kind: Format = _format(file_format)
        if kind == "marg_import" and books != "marg":
            raise ValueError("a Marg purchase import is for Marg's books")
        if end < start:
            raise RunRangeInvalid("the range ends before it starts")
        if (end - start).days >= MAX_RANGE_DAYS:
            raise RunRangeInvalid(f"a run covers at most {MAX_RANGE_DAYS} days")
        version = self.profiles.active_version(tenant_id) if kind == "marg_import" else None
        lower = datetime.combine(start, time(), _INDIA)
        upper = datetime.combine(end + timedelta(days=1), time(), _INDIA)
        run_id = uuid.uuid4()
        with self._sessions() as session:
            bills, unreadable = _approved(session, tenant_id, lower, upper)
            resolution = resolve(session, tenant_id, bills, books)
            ids = [bill.document_id for bill in bills]
            overlaps, before = _overlaps(session, tenant_id, books, ids)
        found = resolution.tasks
        checked: tuple[BillProblem, ...] = ()
        if version is not None:
            fields = LineExtraction.model_fields  # a line_no is counted, never missing
            required = [c.field for c in version.spec.columns
                        if c.required and c.source == "line" and c.field in fields]  # fmt: skip
            checked = preflight(bills, required=required)
            if not checked and not found:
                checked = _unwritable(version, bills, resolution)
        tasks = Tasks(found.suppliers, found.items, (*unreadable, *found.problems, *checked))
        lines = sum(len(bill.invoice.lines) for bill in bills)
        file_key = filename = sha256 = None
        size = None
        if not tasks:
            if version is None:
                data = to_csv(_rows(bills, resolution, books), fieldnames=COLUMNS).encode()
                extension = "csv"
                filename = f"docforge-{books}-{start}-to-{end}-{str(run_id)[:8]}.csv"
            else:
                data = render(version.spec, _booked(bills, resolution))
                extension = version.spec.file_type
                sample = "SAMPLE-" if version.basis == "sample" else ""
                filename = (
                    f"{sample}docforge-marg-import-{start}-to-{end}-{str(run_id)[:8]}.{extension}"
                )
            sha256 = hashlib.sha256(data).hexdigest()
            size = len(data)
            file_key = f"exports/{tenant_id}/{run_id}.{extension}"
            self._store.put(file_key, data, _MEDIA[extension])
        status: Status = "needs_mapping" if tasks else "ready"
        invoices = len(bills) + len(unreadable)
        with self._sessions.begin() as session:
            row = ExportRun(
                id=run_id, tenant_id=tenant_id, target=books, format=kind, range_start=start,
                range_end=end, status=status, invoices=invoices, lines=lines,
                tasks=tasks.to_json() if tasks else None, file_key=file_key, filename=filename,
                sha256=sha256, size_bytes=size, created_by=name[:200],
                profile_version_id=version.id if version else None,
                overlaps=[
                    {"run_id": str(o.run_id), "created_at": o.created_at.isoformat(),
                     "format": o.format, "invoices": o.invoices}
                    for o in overlaps
                ] or None,
            )  # fmt: skip
            session.add(row)
            session.flush()
            if status == "ready" and ids:
                session.execute(
                    insert(ExportRunDocument),
                    [{"tenant_id": tenant_id, "run_id": run_id, "document_id": d} for d in ids],
                )
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="export.created",
                target_type="export_run", target_id=str(run_id),
                details={
                    "target": books, "format": kind, "status": status, "invoices": invoices,
                    "lines": lines, "unmapped_suppliers": len(tasks.suppliers),
                    "unmapped_items": len(tasks.items), "problems": len(tasks.problems),
                    "sha256": sha256, "exported_before": before,
                    "profile_version": version.version_no if version else None,
                },
            )  # fmt: skip
            session.flush()
            session.refresh(row)
            return self._view(row, version)

    @scoped
    def check(
        self,
        tenant_id: uuid.UUID,
        profile_id: uuid.UUID,
        *,
        document_ids: Sequence[uuid.UUID],
        accepted: bytes,
        actor: str,
    ) -> CheckView:
        """Make the profile's file for these approved bills, in the order given, and compare
        it with the file the books accepted for them, cell by cell. Nothing is stored.
        Raises `ProfileNotFound`, `CheckInvalid` or `SampleUnreadable`."""
        ids = list(dict.fromkeys(document_ids))
        if not 1 <= len(ids) <= MAX_CHECK_BILLS:
            raise CheckInvalid(f"Choose 1 to {MAX_CHECK_BILLS} approved bills to compare.")
        latest = self.profiles.get(tenant_id, profile_id).latest
        with self._sessions() as session:
            bills, unreadable = _approved(session, tenant_id, document_ids=ids)
            missing = len(ids) - len(bills) - len(unreadable)
            if missing:
                raise CheckInvalid(
                    f"{missing} of the chosen bills are not approved invoices in this "
                    "workspace: choose only approved bills."
                )
            order = {document_id: at for at, document_id in enumerate(ids)}
            bills.sort(key=lambda bill: order[bill.document_id])
            resolution = resolve(session, tenant_id, bills, "marg")
        found = resolution.tasks
        tasks = Tasks(found.suppliers, found.items, (*unreadable, *found.problems))
        comparison = None
        if not tasks:
            produced = table(latest.spec, _booked(bills, resolution))
            comparison = compare(latest.spec, produced, accepted)
        with self._sessions.begin() as session:
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="export_profile.checked",
                target_type="export_profile", target_id=str(profile_id),
                details={
                    "version": latest.version_no, "bills": len(ids),
                    "cells": comparison.cells if comparison else 0,
                    "equal": comparison.equal if comparison else 0,
                },
            )  # fmt: skip
        return CheckView(_ref(latest), len(ids), tasks or None, comparison)

    @scoped
    def list(self, tenant_id: uuid.UUID, *, limit: int = 50) -> list[RunView]:
        """Newest first."""
        with self._sessions() as session:
            rows = list(
                session.scalars(
                    select(ExportRun)
                    .where(ExportRun.tenant_id == tenant_id)
                    .order_by(ExportRun.created_at.desc(), ExportRun.id.desc())
                    .limit(min(max(limit, 1), MAX_LIST))
                )
            )
        used = {row.profile_version_id for row in rows if row.profile_version_id is not None}
        versions = {v: self.profiles.version(tenant_id, v) for v in used}
        return [
            self._view(row, versions[row.profile_version_id] if row.profile_version_id else None)
            for row in rows
        ]

    @scoped
    def get(self, tenant_id: uuid.UUID, run_id: uuid.UUID) -> RunView:
        """Raises `RunNotFound`."""
        with self._sessions() as session:
            row = _find(session, tenant_id, run_id)
        version = (
            self.profiles.version(tenant_id, row.profile_version_id)
            if row.profile_version_id
            else None
        )
        return self._view(row, version)

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

    @staticmethod
    def _view(row: ExportRun, version: VersionView | None) -> RunView:
        return RunView(
            id=row.id,
            target="tally" if row.target == "tally" else "marg",
            format=_format(row.format),
            start=row.range_start,
            end=row.range_end,
            status="ready" if row.status == "ready" else "needs_mapping",
            invoices=row.invoices,
            lines=row.lines,
            tasks=Tasks.from_json(row.tasks) if row.tasks is not None else None,
            filename=row.filename,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            profile=_ref(version) if version else None,
            overlaps=tuple(
                Overlap(
                    uuid.UUID(o["run_id"]),
                    datetime.fromisoformat(o["created_at"]),
                    _format(o["format"]),
                    int(o["invoices"]),
                )
                for o in row.overlaps or ()
            ),
            created_by=row.created_by,
            created_at=row.created_at,
        )


def media_type(filename: str) -> str:
    return _MEDIA["xlsx" if filename.endswith(".xlsx") else "csv"]


def _find(session: Session, tenant_id: uuid.UUID, run_id: uuid.UUID) -> ExportRun:
    row = session.scalars(
        select(ExportRun).where(ExportRun.tenant_id == tenant_id, ExportRun.id == run_id)
    ).one_or_none()
    if row is None:
        raise RunNotFound(run_id)
    return row
