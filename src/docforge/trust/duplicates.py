"""Bills received twice: an invoice whose bill the workspace already has, as another live
invoice received earlier.

The same IRN (both verified e-invoice QRs) is the same bill: the portal issues one per bill.
So is the same seller GSTIN and invoice number, each as the reading has it after corrections;
numbers are compared without case or whitespace, while "/" and "-" count. Only the later of two
is a duplicate - the first one received is the original - and an earlier bill that was deleted,
or rejected by a reviewer (the second may be its corrected resend), does not count.

Found live on every review read, so it follows corrections on either side, deletions and
rejections with nothing kept in sync. Candidates come from indexes (migration 0040) and are
then compared on their values as corrected.
"""

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import and_, exists, func, literal, or_, select, union
from sqlalchemy.orm import Session, aliased

from docforge.db.models import (
    CORRECTED_NUMBER_KEY,
    INVOICE_NUMBER_KEY,
    SIGNED_IRN,
    Correction,
    Document,
    DocumentVersion,
    EinvoiceCheck,
    Extraction,
    Review,
    live,
)

MatchedOn = Literal["irn", "seller_and_number"]
_CANDIDATES = 20  # earlier bills compared per lookup; more with one number is no longer news
_PATHS = ("invoice_no", "seller.gstin")
_SAME = {"irn": "the same IRN", "seller_and_number": "the same seller GSTIN and invoice number"}


@dataclass(frozen=True)
class Duplicate:
    document_id: uuid.UUID
    filename: str
    matched_on: MatchedOn

    @property
    def reason(self) -> str:
        return f"it may be a duplicate of {self.filename}, which has {_SAME[self.matched_on]}"


def number_key(text: object) -> str | None:
    """An invoice number as compared; `INVOICE_NUMBER_KEY` in SQL."""
    if not isinstance(text, str):
        return None
    return "".join(text.split()).lower() or None


def gstin_key(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    return "".join(text.split()).upper() or None


def find_duplicates(
    session: Session,
    document: Document,
    *,
    seller_gstin: str | None,
    invoice_no: str | None,
    irn: str | None,
) -> tuple[Duplicate, ...]:
    """The earlier bills `document` (an invoice) repeats, by IRN first, then by number."""
    found: dict[uuid.UUID, Duplicate] = {}
    if irn:
        rows = session.execute(
            _earlier(document)
            .join(EinvoiceCheck, EinvoiceCheck.document_version_id == DocumentVersion.id)
            .where(
                EinvoiceCheck.tenant_id == document.tenant_id,
                EinvoiceCheck.status == "verified",
                irn == SIGNED_IRN,
            )
        ).all()
        for other, _ in rows:
            found.setdefault(other.id, Duplicate(other.id, other.filename, "irn"))
    gstin, number = gstin_key(seller_gstin), number_key(invoice_no)
    if gstin and number:
        read = select(Extraction.document_version_id).where(
            Extraction.tenant_id == document.tenant_id, literal(number) == INVOICE_NUMBER_KEY
        )
        corrected = select(Correction.document_version_id).where(
            Correction.tenant_id == document.tenant_id,
            Correction.path == "invoice_no",
            literal(number) == CORRECTED_NUMBER_KEY,
        )
        rows = session.execute(
            _earlier(document).where(DocumentVersion.id.in_(union(read, corrected)))
        ).all()
        for other, version_id in rows:
            seller, their_number = _as_corrected(session, version_id)
            if gstin_key(seller) == gstin and number_key(their_number) == number:
                found.setdefault(other.id, Duplicate(other.id, other.filename, "seller_and_number"))
    return tuple(found.values())


def _earlier(document: Document) -> Any:
    """Live invoices received before `document`, with their newest version, as candidates;
    none whose newest version a reviewer rejected."""
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
    return (
        select(Document, DocumentVersion.id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(
            Document.tenant_id == document.tenant_id,
            live(),
            Document.doc_type == "invoice",
            Document.id != document.id,
            or_(
                Document.created_at < document.created_at,
                and_(Document.created_at == document.created_at, Document.id < document.id),
            ),
            DocumentVersion.version_no == newest,
            ~rejected,
        )
        .order_by(Document.created_at, Document.id)
        .limit(_CANDIDATES)
    )


def _as_corrected(session: Session, version_id: uuid.UUID) -> tuple[object, object]:
    """A version's seller GSTIN and invoice number, each as last corrected, else as read."""
    data = (
        session.scalar(select(Extraction.data).where(Extraction.document_version_id == version_id))
        or {}
    )
    values: dict[str, object] = {
        "invoice_no": (data.get("invoice_no") or {}).get("value"),
        "seller.gstin": ((data.get("seller") or {}).get("gstin") or {}).get("value"),
    }
    for path, text in session.execute(
        select(Correction.path, Correction.new_text)
        .where(Correction.document_version_id == version_id, Correction.path.in_(_PATHS))
        .order_by(Correction.created_at, Correction.id)
    ):
        values[path] = text
    return values["seller.gstin"], values["invoice_no"]
