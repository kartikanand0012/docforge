"""What the checks flag on a bill, in a form a person can read and count.

Three checks are already built and found live on every review read: duplicates
(`trust.duplicates`, a blocker), stock near expiry (`line.shelf_life`, a flag) and Schedule H1
lines (`h1_products`, a flag). Here they are counted for a queue row, listed line by line for a
bill, and summed over a range of days for the workspace's home screen ("it caught 2
duplicates and 3 near-expiry lines on your bills").

The summary follows the same rules on the same effective values (each bill as corrected), so
it never says something the bill itself does not: a duplicate is a later live invoice with the
IRN, or the seller GSTIN and number, of an earlier live invoice not rejected; a near-expiry
line has less than the window left after the bill date; an H1 line names a molecule in force
on the bill's date or a product the workspace marked.
"""

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import cast, exists, func, literal, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.orm import Session, aliased

from docforge.db.models import (
    CORRECTED_KEY,
    INVOICE_NUMBER_KEY,
    SELLER_GSTIN_KEY,
    SIGNED_IRN,
    Correction,
    Document,
    DocumentVersion,
    EinvoiceCheck,
    Extraction,
    Review,
    live,
)
from docforge.extraction.schema import InvoiceExtraction
from docforge.h1_products import all_marks, h1_lines
from docforge.trust.invoice_rules import is_near_expiry, months_left

_INDIA = ZoneInfo("Asia/Kolkata")
SUMMARY_SCAN = 10_000  # invoices counted per summary, at most: the export limit
MAX_RANGE_DAYS = 366  # a summary covers at most a year
_BATCH = 500  # versions whose lines are read per query
# Each line's expiry and product name, in line order, without reading the whole record.
_EXPIRIES = cast(literal("$.lines[*].expiry.value"), JSONPATH)
_NAMES = cast(literal("$.lines[*].product_name.value"), JSONPATH)


@dataclass(frozen=True)
class FlagCounts:
    """One bill's flags, for its queue row."""

    duplicates: int = 0  # earlier bills it may repeat; each one is a blocker
    near_expiry_lines: int = 0
    schedule_h1_lines: int = 0


@dataclass(frozen=True)
class NearExpiryLine:
    """A line whose stock has less than the workspace's window left after the bill date."""

    path: str  # lines[i]
    product_name: str | None
    batch_no: str | None
    expiry: str  # YYYY-MM
    months_left: int  # from the bill date's month; 0 when it expires that month


@dataclass(frozen=True)
class FlagSummary:
    start: date  # India's dates, both included
    end: date
    invoices: int  # invoices received in the range and counted
    duplicates: int  # of those, how many repeat an earlier bill
    near_expiry_lines: int
    near_expiry_bills: int
    schedule_h1_lines: int
    schedule_h1_bills: int
    capped: bool  # more invoices than SUMMARY_SCAN: the counts are then a lower bound


class RangeInvalid(ValueError):
    """A summary's range ends before it starts or is longer than MAX_RANGE_DAYS."""


def near_expiry_lines(invoice: InvoiceExtraction, months: int) -> tuple[NearExpiryLine, ...]:
    """The lines `line.shelf_life` flags, with the months each has left. None without a bill
    date (there is nothing to measure from) or with a window of 0."""
    issued = invoice.invoice_date.value
    if issued is None or months <= 0:
        return ()
    found = []
    for index, line in enumerate(invoice.lines):
        expiry = line.expiry.value
        if expiry is None:
            continue
        remaining = months_left(expiry, issued)
        if is_near_expiry(remaining, months):
            found.append(
                NearExpiryLine(
                    path=f"lines[{index}]",
                    product_name=line.product_name.value,
                    batch_no=line.batch_no.value,
                    expiry=expiry,
                    months_left=remaining,
                )
            )
    return tuple(found)


@dataclass(frozen=True)
class _Bill:
    """What the summary needs of one invoice: its date and its lines' expiries and names."""

    issued: date | None
    expiries: Sequence[str | None]
    names: Sequence[str | None]


@dataclass(frozen=True)
class _Received:
    document_id: uuid.UUID
    created_at: datetime
    version_id: uuid.UUID
    number: str | None  # the invoice number as compared for duplicates
    seller: str | None  # the seller GSTIN as compared for duplicates
    irn: str | None  # from its verified e-invoice QR
    rejected: bool  # its newest version was rejected by a reviewer


def flag_summary(
    session: Session,
    tenant_id: uuid.UUID,
    start: date,
    end: date,
    *,
    window: int,
    today: date,
    effective: Callable[[uuid.UUID], InvoiceExtraction | None],
) -> FlagSummary:
    """The flags on the invoices the workspace received from `start` to `end` (India's dates).

    `effective` gives a version's invoice as corrected; it is called only for versions with
    corrections, the rest are read from their stored values without the whole record.
    """
    if end < start:
        raise RangeInvalid("the range ends before it starts")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise RangeInvalid(f"a range covers at most {MAX_RANGE_DAYS} days")
    lower = datetime.combine(start, time(), _INDIA)
    upper = datetime.combine(end + timedelta(days=1), time(), _INDIA)
    received = _with_key_corrections(session, tenant_id, _received(session, tenant_id, upper))

    seen_numbers: set[tuple[str, str]] = set()
    seen_irns: set[str] = set()
    counted: list[uuid.UUID] = []
    duplicates, capped = 0, False
    for bill in received:
        pair = (bill.seller, bill.number) if bill.seller and bill.number else None
        if bill.created_at >= lower:
            if len(counted) < SUMMARY_SCAN:
                counted.append(bill.version_id)
                repeats = (pair is not None and pair in seen_numbers) or (
                    bill.irn is not None and bill.irn in seen_irns
                )
                duplicates += repeats
            else:
                capped = True
        # A rejected bill is no original: the one after it may be its corrected resend.
        if not bill.rejected:
            if pair is not None:
                seen_numbers.add(pair)
            if bill.irn is not None:
                seen_irns.add(bill.irn)

    marks = all_marks(session, tenant_id)
    near_lines = near_bills = h1_count = h1_bills = 0
    for lines in _bills(session, tenant_id, counted, effective):
        issued = lines.issued
        near = (
            sum(
                1
                for expiry in lines.expiries
                if expiry and is_near_expiry(months_left(expiry, issued), window)
            )
            if issued is not None
            else 0
        )
        h1 = len(h1_lines(lines.names, marks, billed=issued, today=today))
        near_lines, near_bills = near_lines + near, near_bills + (near > 0)
        h1_count, h1_bills = h1_count + h1, h1_bills + (h1 > 0)
    return FlagSummary(
        start=start,
        end=end,
        invoices=len(counted),
        duplicates=duplicates,
        near_expiry_lines=near_lines,
        near_expiry_bills=near_bills,
        schedule_h1_lines=h1_count,
        schedule_h1_bills=h1_bills,
        capped=capped,
    )


def _received(session: Session, tenant_id: uuid.UUID, upper: datetime) -> list[_Received]:
    """Every live invoice received before `upper`, oldest first, by its newest version that
    has been read: the counted ones and the earlier ones a duplicate may repeat."""
    newer = aliased(DocumentVersion)
    newest = (
        select(func.max(newer.version_no))
        .where(newer.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )
    rejected = exists().where(
        Review.document_version_id == DocumentVersion.id, Review.outcome == "rejected"
    )
    irn = (
        select(SIGNED_IRN)
        .where(
            EinvoiceCheck.document_version_id == DocumentVersion.id,
            EinvoiceCheck.tenant_id == tenant_id,
            EinvoiceCheck.status == "verified",
        )
        .correlate(DocumentVersion)
        .scalar_subquery()
    )
    rows = session.execute(
        select(
            Document.id,
            Document.created_at,
            DocumentVersion.id,
            INVOICE_NUMBER_KEY,
            SELLER_GSTIN_KEY,
            irn,
            rejected,
        )
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Extraction, Extraction.document_version_id == DocumentVersion.id)
        .where(
            Document.tenant_id == tenant_id,
            Extraction.tenant_id == tenant_id,
            live(),
            Document.doc_type == "invoice",
            Document.created_at < upper,
            DocumentVersion.version_no == newest,
        )
        .order_by(Document.created_at, Document.id)
    ).all()
    return [
        _Received(
            document_id=row[0],
            created_at=row[1],
            version_id=row[2],
            number=row[3] or None,
            seller=row[4] or None,
            irn=row[5] or None,
            rejected=bool(row[6]),
        )
        for row in rows
    ]


def _with_key_corrections(
    session: Session, tenant_id: uuid.UUID, received: list[_Received]
) -> list[_Received]:
    """Each bill's invoice number and seller GSTIN as last corrected, keyed as
    `trust.duplicates` compares them."""
    by_version = {bill.version_id: index for index, bill in enumerate(received)}
    if not by_version:
        return received
    rows = session.execute(
        select(Correction.document_version_id, Correction.path, CORRECTED_KEY)
        .where(
            Correction.tenant_id == tenant_id,
            Correction.path.in_(("invoice_no", "seller.gstin")),
        )
        .order_by(Correction.created_at, Correction.id)
    ).all()
    out = list(received)
    for version_id, path, key in rows:
        index = by_version.get(version_id)
        if index is None:
            continue
        if path == "invoice_no":
            out[index] = replace(out[index], number=key or None)
        else:
            out[index] = replace(out[index], seller=key or None)
    return out


def _bills(
    session: Session,
    tenant_id: uuid.UUID,
    version_ids: Sequence[uuid.UUID],
    effective: Callable[[uuid.UUID], InvoiceExtraction | None],
) -> Iterable[_Bill]:
    """Each version's date, expiries and product names, as corrected."""
    corrected: set[uuid.UUID] = set()
    for offset in range(0, len(version_ids), _BATCH):
        corrected.update(
            session.scalars(
                select(Correction.document_version_id)
                .where(
                    Correction.tenant_id == tenant_id,
                    Correction.document_version_id.in_(version_ids[offset : offset + _BATCH]),
                )
                .distinct()
            )
        )
    plain = [version_id for version_id in version_ids if version_id not in corrected]
    for offset in range(0, len(plain), _BATCH):
        rows = session.execute(
            select(
                Extraction.data["invoice_date"]["value"].astext,
                func.jsonb_path_query_array(Extraction.data, _EXPIRIES),
                func.jsonb_path_query_array(Extraction.data, _NAMES),
            ).where(
                Extraction.tenant_id == tenant_id,
                Extraction.document_version_id.in_(plain[offset : offset + _BATCH]),
            )
        )
        for issued, expiries, names in rows:
            yield _Bill(
                issued=date.fromisoformat(issued) if issued else None,
                expiries=[e if isinstance(e, str) else None for e in expiries or ()],
                names=[n if isinstance(n, str) else None for n in names or ()],
            )
    for version_id in version_ids:
        if version_id not in corrected:
            continue
        invoice = effective(version_id)
        if invoice is not None:
            yield _Bill(
                issued=invoice.invoice_date.value,
                expiries=[line.expiry.value for line in invoice.lines],
                names=[line.product_name.value for line in invoice.lines],
            )
