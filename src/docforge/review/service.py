"""The review service: what waits for a person, corrections, and signed decisions.

Identity until accounts exist (C6): a reviewer is a row with an email and a hashed PIN, and
every correction and signature re-checks the PIN. Five wrong PINs lock the reviewer for 15
minutes. Every change is written to the audit log; audit entries hold paths, ids and hashes,
never values or reasons, because the log cannot be edited if they must be erased.
"""

import io
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, aliased

from docforge import audit
from docforge.db.models import (
    BATCH_NUMBER,
    Document,
    DocumentVersion,
    Extraction,
    ParseOutput,
    Review,
    Reviewer,
)
from docforge.db.models import (
    Correction as CorrectionRow,
)
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.documents import DocumentNotFound, EventSink, current_match, event_id
from docforge.extraction.coa import CoaExtraction
from docforge.extraction.pipeline import DocumentSpec
from docforge.extraction.purchase_order import PurchaseOrderExtraction
from docforge.extraction.schema import InvoiceExtraction
from docforge.parsing.base import ParsedDocument
from docforge.parsing.raster import render_pages
from docforge.review.revise import (
    Correction,
    Reassessed,
    apply_corrections,
    field_paths,
    reassess,
)
from docforge.review.signing import (
    MEANINGS,
    approval_draft,
    hash_pin,
    record_digest,
    record_hash,
    verify_pin,
)
from docforge.storage import ObjectStore
from docforge.trust.assess import Assessment
from docforge.trust.match import Discrepancy, match_invoice_to_order
from docforge.trust.rules import run_rules

MAX_FAILED_PINS = 5
LOCK_FOR = timedelta(minutes=15)
_MIN_PIN_DIGITS = 6
_PAGE_DPI = 110
_QUEUE_LIMIT = 200
_QUEUE_SCAN = 20  # documents examined per queue entry returned, at most
_CONCURRENT_RENDERS = 3  # page images rendered at once in the API process
_DUMMY_HASH = hash_pin("not-a-real-pin")  # checked for unknown emails, so timing says nothing


class NotAuthenticated(Exception):
    """Unknown reviewer or wrong PIN. Deliberately does not say which."""


class ReviewerLocked(Exception):
    """Too many wrong PINs; try again later."""


class AlreadySigned(Exception):
    """The version has a signed review; it can no longer change."""


class ApprovalBlocked(Exception):
    """Approval needs an override reason: the record still fails a check."""


class NotReviewable(Exception):
    """No extraction that can be reviewed (none yet, made before review existed, or a newer
    version is being processed)."""


class NotPermitted(Exception):
    """The PIN given is not the signed-in reviewer's: people sign only as themselves."""


class RecordChanged(Exception):
    """The record changed after the reviewer saw it; they must look again before signing."""


class PageNotFound(LookupError):
    """The document has no such page."""


@dataclass(frozen=True)
class QueueItem:
    document_id: uuid.UUID
    doc_type: str
    filename: str
    version_no: int
    created_at: datetime
    reasons: tuple[str, ...]
    match_status: str


@dataclass(frozen=True)
class CorrectionView:
    path: str
    old_text: str | None
    new_text: str | None
    reason: str
    reviewer_name: str
    created_at: datetime


@dataclass(frozen=True)
class SignedReview:
    outcome: str
    meaning: str
    reason: str
    override_reason: str | None
    reviewer_name: str
    signed_at: datetime
    record_sha256: str
    draft: dict[str, Any] | None


@dataclass(frozen=True)
class ReviewDetail:
    document_id: uuid.UUID
    doc_type: str
    filename: str
    version_id: uuid.UUID
    version_no: int
    page_count: int
    pages: tuple[dict[str, Any], ...]  # number, width and height in points, for the boxes
    record: dict[str, Any]  # the extraction with corrections applied
    assessment: Assessment
    editable_paths: tuple[str, ...]
    corrections: tuple[CorrectionView, ...]
    match_status: str  # match, mismatch or no_counterpart
    discrepancies: tuple[Discrepancy, ...]
    counterpart_document_id: uuid.UUID | None
    blockers: tuple[str, ...]  # why approval would need an override reason
    review: SignedReview | None
    signature_valid: bool | None  # None until signed: does the stored review match its hash
    record_sha256: str  # of `record`; a signature must name it, so it signs what was seen
    meanings: dict[str, str]  # outcome -> the sentence a signature with that outcome means
    superseded: bool  # a newer version of the document is being processed
    # For an invoice: the certificate found for each billed batch, and whether it is in limits.
    certificates: tuple[dict[str, str], ...] = ()

    @property
    def decision(self) -> str:
        return "review" if self.blockers else "accept"


@dataclass(frozen=True)
class _State:
    document: Document
    version: DocumentVersion
    spec: DocumentSpec[Any]
    raw: BaseModel
    parsed: ParsedDocument
    corrections: list[tuple[CorrectionRow, str]]  # with the reviewer's name
    reassessed: Reassessed
    match_status: str
    discrepancies: tuple[Discrepancy, ...]
    counterpart: uuid.UUID | None
    review: tuple[Review, str] | None
    superseded: bool
    certificates: tuple[dict[str, str], ...]

    @property
    def blockers(self) -> tuple[str, ...]:
        found = list(self.reassessed.assessment.reasons)
        if self.match_status == "mismatch":
            found.append("it does not match its purchase order")
        if self.document.doc_type == "invoice" and self.match_status == "no_counterpart":
            found.append("there is no purchase order on file to compare it with")
        for certificate in self.certificates:
            if certificate["status"] == "out_of_limit":
                found.append(
                    f"the certificate for batch {certificate['batch_no']} "
                    "has results outside their limits"
                )
            elif certificate["status"] == "unverified":
                found.append(
                    f"the certificate for batch {certificate['batch_no']} "
                    "could not be fully checked"
                )
        return tuple(found)


def _now() -> datetime:
    return datetime.now(UTC)


class ReviewService:
    def __init__(
        self,
        sessions: SessionFactory,
        store: ObjectStore,
        specs: Mapping[str, DocumentSpec[Any]],
        *,
        queue_limit: int = _QUEUE_LIMIT,
        events: EventSink | None = None,
    ) -> None:
        self._events = events
        self._sessions = sessions
        self._store = store
        self._specs = dict(specs)
        self._queue_limit = queue_limit
        self._renders = threading.BoundedSemaphore(_CONCURRENT_RENDERS)

    # Reviewers

    @scoped
    def add_reviewer(
        self, tenant_id: uuid.UUID, *, name: str, email: str, pin: str, role: str = "reviewer"
    ) -> uuid.UUID:
        if role not in ("reviewer", "admin"):
            raise ValueError("a reviewer's role is reviewer or admin")
        if len(pin) < _MIN_PIN_DIGITS or not pin.isdigit():
            raise ValueError(f"a PIN must be at least {_MIN_PIN_DIGITS} digits")
        email = email.strip().lower()
        with self._sessions.begin() as session:
            exists = session.scalar(
                select(Reviewer.id).where(Reviewer.tenant_id == tenant_id, Reviewer.email == email)
            )
            if exists is not None:
                raise ValueError(f"a reviewer with the email {email} already exists")
            reviewer = Reviewer(
                tenant_id=tenant_id,
                name=name.strip(),
                email=email,
                pin_hash=hash_pin(pin),
                role=role,
            )
            session.add(reviewer)
            session.flush()
            return reviewer.id

    @scoped
    def deactivate_reviewer(self, tenant_id: uuid.UUID, email: str) -> None:
        """Stop a reviewer from correcting or signing. Their past signatures stay."""
        with self._sessions.begin() as session:
            reviewer = session.scalar(
                select(Reviewer).where(
                    Reviewer.tenant_id == tenant_id, Reviewer.email == email.strip().lower()
                )
            )
            if reviewer is None:
                raise LookupError(f"no reviewer with the email {email}")
            reviewer.deactivated_at = _now()

    def _acting_as(self, tenant_id: uuid.UUID, email: str, acting: uuid.UUID | None) -> None:
        """Refuse before any PIN is checked when the email is not the signed-in reviewer's,
        so a reviewer cannot test, or lock, someone else's PIN."""
        if acting is None:
            return
        with self._sessions() as session:
            owner = session.scalar(
                select(Reviewer.id).where(
                    Reviewer.tenant_id == tenant_id, Reviewer.email == email.strip().lower()
                )
            )
        if owner != acting:
            raise NotPermitted

    def _authenticate(self, tenant_id: uuid.UUID, email: str, pin: str) -> Reviewer:
        """The reviewer, if the PIN is right. A wrong PIN is counted, in its own transaction."""
        with self._sessions.begin() as session:
            reviewer = session.scalar(
                select(Reviewer)
                .where(Reviewer.tenant_id == tenant_id, Reviewer.email == email.strip().lower())
                .with_for_update(key_share=True)
            )
            if reviewer is None or reviewer.deactivated_at is not None:
                verify_pin(pin, _DUMMY_HASH)
                raise NotAuthenticated
            now = _now()
            if reviewer.locked_until is not None and reviewer.locked_until > now:
                raise ReviewerLocked
            if not verify_pin(pin, reviewer.pin_hash):
                # Recorded, so a guessing attempt leaves a trail; nothing about the PIN is kept.
                audit.append(
                    session,
                    tenant_id=tenant_id,
                    actor="api",
                    action="reviewer.pin_failed",
                    target_type="reviewer",
                    target_id=str(reviewer.id),
                    details={"attempt": reviewer.failed_attempts + 1},
                )
                reviewer.failed_attempts += 1
                if reviewer.failed_attempts >= MAX_FAILED_PINS:
                    reviewer.failed_attempts = 0
                    reviewer.locked_until = now + LOCK_FOR
                failed = True
            else:
                reviewer.failed_attempts = 0
                reviewer.locked_until = None
                failed = False
            session.flush()
            session.expunge(reviewer)
        if failed:
            raise NotAuthenticated
        return reviewer

    # Reading

    @scoped
    def queue(self, tenant_id: uuid.UUID) -> list[QueueItem]:
        """Documents whose newest extraction needs a person and has no signed review.

        Signed documents are left out in the query; whether the rest need a person depends
        on corrections and the order match, so it is decided per document, scanning at most
        `_QUEUE_SCAN` documents for each entry the queue can hold.
        """
        newest = aliased(DocumentVersion)
        signed = (
            select(Review.id)
            .join(DocumentVersion, DocumentVersion.id == Review.document_version_id)
            .where(
                DocumentVersion.document_id == Document.id,
                DocumentVersion.version_no
                == select(func.max(newest.version_no))
                .where(newest.document_id == Document.id)
                .scalar_subquery(),
            )
            .correlate(Document)
        )
        items: list[QueueItem] = []
        with self._sessions() as session:
            documents = session.scalars(
                select(Document)
                .where(
                    Document.tenant_id == tenant_id,
                    Document.status == "extracted",
                    ~exists(signed),
                )
                .order_by(Document.created_at, Document.id)
                .limit(self._queue_limit * _QUEUE_SCAN)
            ).all()
            for document in documents:
                try:
                    state = self._state(session, document)
                except NotReviewable:
                    continue
                if state.review is None and state.blockers:
                    items.append(
                        QueueItem(
                            document_id=document.id,
                            doc_type=document.doc_type,
                            filename=document.filename,
                            version_no=state.version.version_no,
                            created_at=state.version.created_at,
                            reasons=state.blockers,
                            match_status=state.match_status,
                        )
                    )
                    if len(items) >= self._queue_limit:
                        break
        return items

    @scoped
    def detail(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> ReviewDetail:
        with self._sessions() as session:
            return self._detail(self._state(session, self._find(session, tenant_id, document_id)))

    @scoped
    def page_image(self, tenant_id: uuid.UUID, document_id: uuid.UUID, page: int) -> bytes:
        """Page `page` of the original as uploaded, as PNG: what the reviewer compares with."""
        with self._sessions() as session:
            document = self._find(session, tenant_id, document_id)
            key, pages = document.storage_key, document.page_count or 0
        if not 1 <= page <= pages:
            raise PageNotFound(f"the document has no page {page}")
        with self._renders:  # rendering holds the PDF library's lock; keep a queue short
            (image,) = render_pages(self._store.get(key), _PAGE_DPI, page, page)
            out = io.BytesIO()
            image.save(out, format="PNG", optimize=True)
        return out.getvalue()

    @scoped
    def export(
        self, tenant_id: uuid.UUID, *, include_records: bool = False, limit: int = 10_000
    ) -> list[dict[str, Any]]:
        """Signed records, newest first: one flat row each, and the record itself if asked."""
        with self._sessions() as session:
            rows = session.execute(
                select(Review, DocumentVersion, Document)
                .join(DocumentVersion, DocumentVersion.id == Review.document_version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Review.tenant_id == tenant_id)
                .order_by(Review.signed_at.desc())
                .limit(limit)
            ).all()
        out: list[dict[str, Any]] = []
        for signed, version, document in rows:
            record: dict[str, Any] = signed.data.get("record") or {}
            invoice = document.doc_type == "invoice"

            def value(*path: str, record: dict[str, Any] = record) -> Any:
                node: Any = record
                for part in path:
                    node = node.get(part) if isinstance(node, dict) else None
                return node.get("value") if isinstance(node, dict) else None

            row: dict[str, Any] = {
                "document_id": str(document.id),
                "doc_type": document.doc_type,
                "filename": document.filename,
                "version_no": version.version_no,
                "outcome": signed.outcome,
                "signed_by": (signed.data.get("signer") or {}).get("name"),
                "signed_at": signed.signed_at.isoformat(),
                "invoice_no": value("invoice_no") if invoice else None,
                "po_no": value("po_no"),
                "supplier_gstin": value("seller", "gstin") if invoice else value("supplier_gstin"),
                "buyer_gstin": value("buyer", "gstin"),
                "grand_total": value("totals", "grand_total") if invoice else None,
                "override_reason": signed.override_reason,
                "record_sha256": signed.record_sha256,
            }
            if include_records:
                row["record"] = record
                row["draft"] = signed.data.get("draft")
            out.append(row)
        return out

    # Changing

    @scoped
    def correct(
        self,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        path: str,
        text: str | None,
        reason: str,
        email: str,
        pin: str,
        acting_reviewer_id: uuid.UUID | None = None,
    ) -> ReviewDetail:
        """Change (or confirm, by giving the same text) one field's printed value.

        `acting_reviewer_id`, when given, is the signed-in reviewer: the PIN must be theirs.
        """
        if not reason.strip():
            raise ValueError("a correction needs a reason")
        with self._sessions() as session:
            self._find(session, tenant_id, document_id)  # unknown documents before PIN checks
        self._acting_as(tenant_id, email, acting_reviewer_id)
        reviewer = self._authenticate(tenant_id, email, pin)
        if acting_reviewer_id is not None and reviewer.id != acting_reviewer_id:
            raise NotPermitted
        with self._sessions.begin() as session:
            document = self._find(session, tenant_id, document_id, lock=True)
            state = self._changeable(self._state(session, document))
            current = state.reassessed.raw.model_dump()
            apply_corrections(state.raw, [Correction(path=path, text=text)])  # validates path
            old = _text_at(current, path)
            session.add(
                CorrectionRow(
                    tenant_id=tenant_id,
                    document_version_id=state.version.id,
                    reviewer_id=reviewer.id,
                    path=path,
                    old_text=old,
                    new_text=text,
                    reason=reason.strip(),
                )
            )
            audit.append(
                session,
                tenant_id=tenant_id,
                actor=f"reviewer:{reviewer.id}",
                action="review.corrected",
                target_type="document",
                target_id=str(document.id),
                details={
                    "path": path,
                    "version_no": state.version.version_no,
                    "changed": old != text,
                },
            )
        return self.detail(tenant_id, document_id)

    @scoped
    def sign(
        self,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        outcome: str,
        meaning: str,
        reason: str,
        override_reason: str | None,
        expected_record_sha256: str,
        email: str,
        pin: str,
        acting_reviewer_id: uuid.UUID | None = None,
    ) -> SignedReview:
        """Approve or reject the record under the reviewer's signature.

        `expected_record_sha256` is the `record_sha256` of the record the reviewer was shown;
        if the record has changed since, nothing is signed.
        """
        if outcome not in ("approved", "rejected"):
            raise ValueError("outcome must be approved or rejected")
        if not reason.strip():
            raise ValueError("a signature needs a reason")
        override = override_reason.strip() if override_reason and override_reason.strip() else None
        with self._sessions() as session:
            self._find(session, tenant_id, document_id)
        self._acting_as(tenant_id, email, acting_reviewer_id)
        reviewer = self._authenticate(tenant_id, email, pin)
        if acting_reviewer_id is not None and reviewer.id != acting_reviewer_id:
            raise NotPermitted
        with self._sessions.begin() as session:
            document = self._find(session, tenant_id, document_id, lock=True)
            state = self._changeable(self._state(session, document))
            required = MEANINGS[document.doc_type][outcome]
            if meaning.strip() != required:
                raise ValueError(f"the meaning of this signature must be: {required}")
            record = state.reassessed.extraction.model_dump(mode="json")
            if record_digest(record) != expected_record_sha256:
                raise RecordChanged
            blockers = state.blockers
            if outcome == "approved" and blockers and override is None:
                raise ApprovalBlocked(
                    "approval needs an override reason because " + "; ".join(blockers)
                )
            signed_at = _now()
            signer = {"id": str(reviewer.id), "name": reviewer.name, "email": reviewer.email}
            digest = record_hash(
                record,
                **_signed(
                    document.id,
                    state.version.id,
                    signer,
                    outcome,
                    required,
                    reason.strip(),
                    override,
                    signed_at,
                ),
            )
            extraction = state.reassessed.extraction
            draft = (
                approval_draft(
                    extraction, reviewer=reviewer.name, signed_at=signed_at, record_sha256=digest
                )
                if outcome == "approved" and isinstance(extraction, InvoiceExtraction)
                else None
            )
            session.add(
                Review(
                    tenant_id=tenant_id,
                    document_version_id=state.version.id,
                    reviewer_id=reviewer.id,
                    outcome=outcome,
                    meaning=meaning.strip(),
                    reason=reason.strip(),
                    override_reason=override,
                    record_sha256=digest,
                    data={
                        "record": record,
                        "draft": draft,
                        "blockers": list(blockers),
                        "signer": signer,
                    },
                    signed_at=signed_at,
                )
            )
            if self._events is not None:
                session.flush()
                self._events.emit(
                    session,
                    tenant_id,
                    "review.signed",
                    {
                        "document_id": str(document.id),
                        "doc_type": document.doc_type,
                        "version_no": state.version.version_no,
                        "outcome": outcome,
                        "meaning": required,
                        "signed_by": reviewer.name,
                        "signed_at": signed_at.isoformat(),
                        "record_sha256": digest,
                        "draft": draft,
                    },
                    event_id=event_id("review.signed", state.version.id),
                )
            audit.append(
                session,
                tenant_id=tenant_id,
                actor=f"reviewer:{reviewer.id}",
                action="review.signed",
                target_type="document",
                target_id=str(document.id),
                details={
                    "outcome": outcome,
                    "version_no": state.version.version_no,
                    "record_sha256": digest,
                    "overridden": override is not None,
                },
            )
        return SignedReview(
            outcome=outcome,
            meaning=meaning.strip(),
            reason=reason.strip(),
            override_reason=override,
            reviewer_name=reviewer.name,
            signed_at=signed_at,
            record_sha256=digest,
            draft=draft,
        )

    # Internals

    @staticmethod
    def _find(
        session: Session, tenant_id: uuid.UUID, document_id: uuid.UUID, *, lock: bool = False
    ) -> Document:
        query = select(Document).where(Document.tenant_id == tenant_id, Document.id == document_id)
        document = session.scalar(query.with_for_update(key_share=True) if lock else query)
        if document is None:
            raise DocumentNotFound(document_id)
        return document

    @staticmethod
    def _changeable(state: _State) -> _State:
        if state.review is not None:
            raise AlreadySigned
        if state.superseded:
            raise NotReviewable("a newer version of this document is being processed")
        return state

    def _effective(
        self, session: Session, version_id: uuid.UUID, doc_type: str
    ) -> BaseModel | None:
        """A version's record with its corrections applied; as extracted if it has no reply."""
        row = session.execute(
            select(Extraction, ParseOutput)
            .join(ParseOutput, ParseOutput.document_version_id == Extraction.document_version_id)
            .where(Extraction.document_version_id == version_id)
        ).first()
        spec = self._specs.get(doc_type)
        if row is None or spec is None:
            return None
        extraction, parse_output = row
        if extraction.raw is None:
            schemas: dict[str, type[BaseModel]] = {
                "invoice": InvoiceExtraction,
                "purchase_order": PurchaseOrderExtraction,
                "coa": CoaExtraction,
            }
            schema = schemas[doc_type]
            return schema.model_validate(extraction.data)
        corrections = session.scalars(
            select(CorrectionRow)
            .where(CorrectionRow.document_version_id == version_id)
            .order_by(CorrectionRow.created_at, CorrectionRow.id)
        )
        return reassess(
            spec,
            spec.raw_schema.model_validate(extraction.raw),
            ParsedDocument.model_validate(parse_output.data),
            [Correction(path=c.path, text=c.new_text) for c in corrections],
        ).extraction

    def _state(self, session: Session, document: Document) -> _State:
        newest_version = session.scalar(
            select(func.max(DocumentVersion.version_no)).where(
                DocumentVersion.document_id == document.id
            )
        )
        row = session.execute(
            select(DocumentVersion, Extraction, ParseOutput)
            .join(Extraction, Extraction.document_version_id == DocumentVersion.id)
            .join(ParseOutput, ParseOutput.document_version_id == DocumentVersion.id)
            .where(
                DocumentVersion.document_id == document.id,
                Extraction.tenant_id == document.tenant_id,
            )
            .order_by(DocumentVersion.version_no.desc())
            .limit(1)
        ).first()
        spec = self._specs.get(document.doc_type)
        if row is None or spec is None or row[1].raw is None:
            raise NotReviewable(document.id)
        version, extraction, parse_output = row
        raw = spec.raw_schema.model_validate(extraction.raw)
        parsed = ParsedDocument.model_validate(parse_output.data)
        corrections = [
            (row.Correction, row.name)
            for row in session.execute(
                select(CorrectionRow, Reviewer.name)
                .join(Reviewer, Reviewer.id == CorrectionRow.reviewer_id)
                .where(CorrectionRow.document_version_id == version.id)
                .order_by(CorrectionRow.created_at, CorrectionRow.id)
            ).all()
        ]
        reassessed = reassess(
            spec, raw, parsed, [Correction(path=c.path, text=c.new_text) for c, _ in corrections]
        )
        match, counterpart = current_match(session, document.tenant_id, version.id)
        discrepancies: tuple[Discrepancy, ...] = ()
        status = "no_counterpart"
        if match is not None:
            other_id = (
                match.order_version_id
                if match.invoice_version_id == version.id
                else match.invoice_version_id
            )
            other_type = "purchase_order" if document.doc_type == "invoice" else "invoice"
            # The counterpart as corrected, not as first extracted.
            other = self._effective(session, other_id, other_type)
            if other is not None:
                mine = reassessed.extraction
                if isinstance(mine, InvoiceExtraction):
                    assert isinstance(other, PurchaseOrderExtraction)  # noqa: S101
                    found = match_invoice_to_order(mine, other)
                else:
                    assert isinstance(other, InvoiceExtraction)  # noqa: S101
                    assert isinstance(mine, PurchaseOrderExtraction)  # noqa: S101
                    found = match_invoice_to_order(other, mine)
                discrepancies = found
                status = "mismatch" if any(d.severity == "error" for d in found) else "match"
        signed = session.execute(
            select(Review, Reviewer.name)
            .join(Reviewer, Reviewer.id == Review.reviewer_id)
            .where(Review.document_version_id == version.id)
        ).first()
        return _State(
            document=document,
            version=version,
            spec=spec,
            raw=raw,
            parsed=parsed,
            corrections=corrections,
            reassessed=reassessed,
            match_status=status,
            discrepancies=discrepancies,
            counterpart=counterpart,
            review=(signed[0], signed[1]) if signed is not None else None,
            superseded=newest_version != version.version_no,
            certificates=self._certificates(session, document, reassessed.extraction),
        )

    def _certificates(
        self, session: Session, document: Document, extraction: BaseModel
    ) -> tuple[dict[str, str], ...]:
        """For an invoice, the newest certificate of analysis on file for each billed batch."""
        if not isinstance(extraction, InvoiceExtraction) or "coa" not in self._specs:
            return ()
        # Batch numbers are compared without regard to case or surrounding spaces; the
        # product must be the same too, since two makers can use the same batch number.
        lines = {
            _batch_key(line.batch_no.value): (line.batch_no.value, line.product_name.value)
            for line in extraction.lines
            if line.batch_no.value
        }
        if not lines:
            return ()
        newer = aliased(DocumentVersion)
        newest = (
            select(func.max(newer.version_no))
            .where(newer.document_id == Document.id)
            .correlate(Document)
            .scalar_subquery()
        )
        rows = session.execute(
            select(Document.id, DocumentVersion.id, BATCH_NUMBER)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .join(Extraction, Extraction.document_version_id == DocumentVersion.id)
            .where(
                Document.tenant_id == document.tenant_id,
                Extraction.tenant_id == document.tenant_id,
                Document.doc_type == "coa",
                DocumentVersion.version_no == newest,
                func.upper(func.btrim(BATCH_NUMBER)).in_(list(lines)),
            )
            .order_by(DocumentVersion.created_at.desc())
        ).all()
        found: dict[str, dict[str, str]] = {}
        for coa_document, coa_version, batch in rows:
            key = _batch_key(batch)
            if key in found:
                continue  # the newest certificate for a batch counts
            effective = self._effective(session, coa_version, "coa")
            coa = effective if isinstance(effective, CoaExtraction) else None
            printed, product = lines[key]
            if coa is not None and _product_key(coa.product_name.value) != _product_key(product):
                continue  # the same batch number for another product
            checks = (
                []
                if coa is None
                else [
                    result.outcome
                    for result in run_rules(self._specs["coa"].rules, coa)
                    if result.rule_id == "coa.result_within_limit"
                ]
            )
            status = (
                "out_of_limit"
                if "failed" in checks
                else ("within_limits" if checks and set(checks) == {"passed"} else "unverified")
            )
            found[key] = {
                "batch_no": printed or batch,
                "document_id": str(coa_document),
                "status": status,
            }
        return tuple(found[key] for key in sorted(lines) if key in found)

    @staticmethod
    def _detail(state: _State) -> ReviewDetail:
        record = state.reassessed.extraction.model_dump(mode="json")
        review, valid = None, None
        if state.review is not None:
            row, name = state.review
            review = SignedReview(
                outcome=row.outcome,
                meaning=row.meaning,
                reason=row.reason,
                override_reason=row.override_reason,
                reviewer_name=name,
                signed_at=row.signed_at,
                record_sha256=row.record_sha256,
                draft=row.data.get("draft"),
            )
            # Recomputed from what was stored, so a change to any signed part shows; the live
            # record is not used, so a later change to the normaliser does not void signatures.
            valid = (
                record_hash(
                    row.data.get("record") or {},
                    **_signed(
                        state.document.id,
                        state.version.id,
                        row.data.get("signer"),
                        row.outcome,
                        row.meaning,
                        row.reason,
                        row.override_reason,
                        row.signed_at,
                    ),
                )
                == row.record_sha256
            )
        return ReviewDetail(
            document_id=state.document.id,
            doc_type=state.document.doc_type,
            filename=state.document.filename,
            version_id=state.version.id,
            version_no=state.version.version_no,
            page_count=len(state.parsed.pages),
            pages=tuple(page.model_dump() for page in state.parsed.pages),
            record=record,
            assessment=state.reassessed.assessment,
            editable_paths=tuple(field_paths(state.raw.model_dump())),
            corrections=tuple(
                CorrectionView(
                    path=c.path,
                    old_text=c.old_text,
                    new_text=c.new_text,
                    reason=c.reason,
                    reviewer_name=name,
                    created_at=c.created_at,
                )
                for c, name in state.corrections
            ),
            match_status=state.match_status,
            discrepancies=state.discrepancies,
            counterpart_document_id=state.counterpart,
            blockers=state.blockers,
            review=review,
            signature_valid=valid,
            record_sha256=record_digest(record),
            meanings=dict(MEANINGS[state.document.doc_type]),
            superseded=state.superseded,
            certificates=state.certificates,
        )


def _batch_key(batch: str | None) -> str:
    return (batch or "").strip().upper()


def _product_key(name: str | None) -> str:
    return " ".join((name or "").lower().split())


def _signed(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    signer: Any,
    outcome: str,
    meaning: str,
    reason: str,
    override_reason: str | None,
    signed_at: datetime,
) -> dict[str, Any]:
    """Everything a signature covers besides the record."""
    return {
        "document_id": str(document_id),
        "version_id": str(version_id),
        "signer": signer,
        "outcome": outcome,
        "meaning": meaning,
        "reason": reason,
        "override_reason": override_reason,
        "signed_at": signed_at.astimezone(UTC).isoformat(),
    }


def _text_at(data: dict[str, Any], path: str) -> str | None:
    node: Any = data
    for part in path.replace("]", "").replace("[", ".").split("."):
        node = node[int(part)] if part.isdigit() else node[part]
    text: str | None = node["text"]
    return text


__all__: Sequence[str] = (
    "AlreadySigned",
    "ApprovalBlocked",
    "NotAuthenticated",
    "NotPermitted",
    "NotReviewable",
    "PageNotFound",
    "RecordChanged",
    "ReviewDetail",
    "ReviewService",
    "ReviewerLocked",
)
