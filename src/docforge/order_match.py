"""An invoice and the purchase order it was billed against, paired.

A pair is found three ways, and each is stored as a comparison (`matches`, append-only):

- when either document is read and the other is already on file (the worker);
- when a reviewer corrects a value pairing depends on (the order number, the seller or
  supplier, the buyer), so a misread order number no longer leaves the two apart for good;
- by hand ("Link order"), with a reason, when nothing on the page links them.

Pairing compares each side's values as last corrected, never only as first read. The order
number alone is whatever a page says; the two must also name the same buyer and supplier.
The newest comparison of a version is its pair (`documents.current_match`), so a later link by
hand, or by a correction, replaces an earlier one; a hand link is a row of its own each time.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, literal, select, union
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from docforge import audit
from docforge.db.models import (
    ORDER_NUMBER,
    Correction,
    Document,
    DocumentVersion,
    Extraction,
    MatchRecord,
    live,
)
from docforge.extraction.normalize import clean_text
from docforge.extraction.purchase_order import PurchaseOrderExtraction
from docforge.extraction.schema import InvoiceExtraction
from docforge.trust.match import company_key, match_invoice_to_order

COUNTERPART = {"invoice": "purchase_order", "purchase_order": "invoice"}
# The fields pairing reads: correcting one of them looks for the counterpart again.
MATCH_PATHS = frozenset(
    {"po_no", "seller.gstin", "seller.name", "buyer.gstin", "supplier_gstin", "supplier_name"}
)
_CANDIDATES = 20  # documents with the order number compared per lookup
ORDER_CANDIDATES = 50  # orders offered for a link by hand


@dataclass(frozen=True)
class Parties:
    supplier_gstin: str | None
    supplier_name: str | None  # as compared: see `company_key`
    buyer_gstin: str | None

    def same_supplier(self, other: "Parties") -> bool:
        """By GSTIN when both print one, by name when either does not (many orders leave out
        the supplier's GSTIN)."""
        if self.supplier_gstin is not None and other.supplier_gstin is not None:
            return self.supplier_gstin == other.supplier_gstin
        return self.supplier_name is not None and self.supplier_name == other.supplier_name

    def pair_with(self, other: "Parties") -> bool:
        """The same buyer by GSTIN, and the same supplier."""
        if self.buyer_gstin is None or self.buyer_gstin != other.buyer_gstin:
            return False
        return self.same_supplier(other)


@dataclass(frozen=True)
class Keys:
    """One version's values that pairing reads, as last corrected."""

    po_no: str | None
    parties: Parties
    supplier_name: str | None  # as printed (or corrected), for showing


@dataclass(frozen=True)
class OrderCandidate:
    document_id: uuid.UUID
    filename: str
    po_no: str | None
    supplier_name: str | None
    created_at: datetime
    same_supplier: bool  # the invoice's seller, by GSTIN or name


# Each path as stored in an extraction's data, for both document types.
_FIELDS = {
    "po_no": ("po_no",),
    "seller.gstin": ("seller", "gstin"),
    "seller.name": ("seller", "name"),
    "buyer.gstin": ("buyer", "gstin"),
    "supplier_gstin": ("supplier_gstin",),
    "supplier_name": ("supplier_name",),
}


def keys(
    session: Session, tenant_id: uuid.UUID, versions: Mapping[uuid.UUID, str]
) -> dict[uuid.UUID, Keys]:
    """The pairing values of each version (id -> its document type), as last corrected. A
    corrected value is read as the normaliser reads these fields (`clean_text`)."""
    if not versions:
        return {}
    ids = list(versions)
    values: dict[uuid.UUID, dict[str, str | None]] = {}
    for version_id, data in session.execute(
        select(Extraction.document_version_id, Extraction.data).where(
            Extraction.tenant_id == tenant_id, Extraction.document_version_id.in_(ids)
        )
    ):
        values[version_id] = {path: _at(data, parts) for path, parts in _FIELDS.items()}
    for version_id, path, text in session.execute(
        select(Correction.document_version_id, Correction.path, Correction.new_text)
        .where(
            Correction.tenant_id == tenant_id,
            Correction.document_version_id.in_(ids),
            Correction.path.in_(MATCH_PATHS),
        )
        .order_by(Correction.created_at, Correction.id)
    ):
        if version_id in values:
            values[version_id][path] = clean_text(text) if text is not None else None
    return {
        version_id: _keys(fields, invoice=versions[version_id] == "invoice")
        for version_id, fields in values.items()
    }


def _at(data: Mapping[str, Any], parts: Sequence[str]) -> str | None:
    node: Any = data
    for part in parts:
        node = node.get(part) if isinstance(node, Mapping) else None
    value = node.get("value") if isinstance(node, Mapping) else None
    return value if isinstance(value, str) and value else None


def _keys(fields: Mapping[str, str | None], *, invoice: bool) -> Keys:
    supplier = fields["seller.name" if invoice else "supplier_name"]
    return Keys(
        po_no=fields["po_no"],
        parties=Parties(
            supplier_gstin=fields["seller.gstin" if invoice else "supplier_gstin"],
            supplier_name=company_key(supplier),
            buyer_gstin=fields["buyer.gstin"],
        ),
        supplier_name=supplier,
    )


def _newest() -> Any:
    newer = aliased(DocumentVersion)
    return (
        select(func.max(newer.version_no))
        .where(newer.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )


def find_counterpart(
    session: Session, document: Document, version_id: uuid.UUID
) -> tuple[Document, uuid.UUID] | None:
    """The live counterpart document (and its newest version) whose order number, buyer and
    supplier agree with this version's, each as last corrected; None if there is none."""
    other_type = COUNTERPART.get(document.doc_type)
    if other_type is None:
        return None
    tenant = document.tenant_id
    mine = keys(session, tenant, {version_id: document.doc_type}).get(version_id)
    if mine is None or mine.po_no is None:
        return None
    # The number as read (expression index `ix_extractions_po_no`) or as corrected
    # (`ix_corrections_po_no`); both are compared again below, as last corrected.
    read = select(Extraction.document_version_id).where(
        Extraction.tenant_id == tenant, literal(mine.po_no) == ORDER_NUMBER
    )
    corrected = select(Correction.document_version_id).where(
        Correction.tenant_id == tenant,
        Correction.path == "po_no",
        Correction.new_text == mine.po_no,
    )
    rows = session.execute(
        select(Document, DocumentVersion.id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(
            Document.tenant_id == tenant,
            live(),
            Document.doc_type == other_type,
            Document.id != document.id,
            DocumentVersion.version_no == _newest(),
            DocumentVersion.id.in_(union(read, corrected)),
        )
        .order_by(DocumentVersion.created_at.desc())
        .limit(_CANDIDATES)
    ).all()
    theirs = keys(session, tenant, {row[1]: other_type for row in rows})
    for other, other_version in rows:
        found = theirs.get(other_version)
        if found and found.po_no == mine.po_no and mine.parties.pair_with(found.parties):
            return other, other_version
    return None


def record_match(
    session: Session,
    *,
    invoice: tuple[Document, uuid.UUID],
    order: tuple[Document, uuid.UUID],
    records: tuple[InvoiceExtraction, PurchaseOrderExtraction],
    actor: str,
    manual: bool = False,
    reason: str | None = None,
    linked_by: str | None = None,
) -> bool:
    """Store the comparison of an invoice version with an order version and audit it on both
    documents. An automatic pair is stored once (False when it already was); a link by hand
    is stored every time, so it is always the newest."""
    discrepancies = match_invoice_to_order(*records)
    decision = "mismatch" if any(d.severity == "error" for d in discrepancies) else "match"
    data: dict[str, Any] = {"discrepancies": [d.model_dump(mode="json") for d in discrepancies]}
    if manual:
        data |= {"reason": reason, "linked_by": linked_by}
    statement = insert(MatchRecord).values(
        tenant_id=invoice[0].tenant_id,
        invoice_version_id=invoice[1],
        order_version_id=order[1],
        decision=decision,
        data=data,
        manual=manual,
    )
    if not manual:
        statement = statement.on_conflict_do_nothing(
            index_elements=["invoice_version_id", "order_version_id"],
            index_where=~MatchRecord.manual,  # as the index says it: NOT manual
        )
    if session.execute(statement.returning(MatchRecord.id)).scalar_one_or_none() is None:
        return False
    action = "match.linked" if manual else "match.created"
    for subject, counterpart in ((invoice[0], order[0]), (order[0], invoice[0])):
        audit.append(
            session,
            tenant_id=subject.tenant_id,
            actor=actor,
            action=action,
            target_type="document",
            target_id=str(subject.id),
            details={
                "counterpart_document_id": str(counterpart.id),
                "decision": decision,
                "discrepancies": len(discrepancies),
            },
        )
    return True


def newest_read_version(session: Session, document: Document) -> uuid.UUID | None:
    """The document's newest version, if it has been read; None while it is being read."""
    newest = (
        select(func.max(DocumentVersion.version_no))
        .where(DocumentVersion.document_id == document.id)
        .scalar_subquery()
    )
    return session.scalar(
        select(DocumentVersion.id)
        .join(Extraction, Extraction.document_version_id == DocumentVersion.id)
        .where(
            DocumentVersion.document_id == document.id,
            Extraction.tenant_id == document.tenant_id,
            DocumentVersion.version_no == newest,
        )
    )


def order_candidates(
    session: Session, invoice: Document, invoice_version_id: uuid.UUID
) -> list[OrderCandidate]:
    """Live purchase orders that have been read, the invoice's seller's first, then newest
    first; at most `ORDER_CANDIDATES`."""
    tenant = invoice.tenant_id
    mine = keys(session, tenant, {invoice_version_id: "invoice"}).get(invoice_version_id)
    rows = session.execute(
        select(Document, DocumentVersion.id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Extraction, Extraction.document_version_id == DocumentVersion.id)
        .where(
            Document.tenant_id == tenant,
            Extraction.tenant_id == tenant,
            live(),
            Document.doc_type == "purchase_order",
            DocumentVersion.version_no == _newest(),
        )
        .order_by(Document.created_at.desc(), Document.id)
        .limit(ORDER_CANDIDATES)
    ).all()
    theirs = keys(session, tenant, {row[1]: "purchase_order" for row in rows})
    found = []
    for order, version_id in rows:
        their = theirs.get(version_id)
        found.append(
            OrderCandidate(
                document_id=order.id,
                filename=order.filename,
                po_no=their.po_no if their else None,
                supplier_name=their.supplier_name if their else None,
                created_at=order.created_at,
                same_supplier=bool(mine and their and mine.parties.same_supplier(their.parties)),
            )
        )
    return sorted(found, key=lambda c: not c.same_supplier)  # stable: newest first within
