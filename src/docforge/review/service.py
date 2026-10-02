"""The review service: what waits for a person, corrections, and signed decisions.

Identity until accounts exist (C6): a reviewer is a row with an email and a hashed PIN, and
every correction and signature re-checks the PIN. Five wrong PINs lock the reviewer for 15
minutes. Every change is written to the audit log; audit entries hold paths, ids and hashes,
never values or reasons, because the log cannot be edited if they must be erased.
"""

import io
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from docforge import audit
from docforge.db.models import (
    Correction as CorrectionRow,
)
from docforge.db.models import (
    Document,
    DocumentVersion,
    Extraction,
    ParseOutput,
    Review,
    Reviewer,
)
from docforge.db.session import SessionFactory
from docforge.documents import DocumentNotFound, current_match
from docforge.extraction.pipeline import DocumentSpec
from docforge.extraction.purchase_order import PurchaseOrderExtraction
from docforge.extraction.schema import InvoiceExtraction
from docforge.parsing.base import ParsedDocument
from docforge.parsing.raster import render_pages
from docforge.review.revise import Correction, Reassessed, apply_corrections, reassess
from docforge.review.signing import approval_draft, hash_pin, record_hash, verify_pin
from docforge.storage import ObjectStore
from docforge.trust.assess import Assessment
from docforge.trust.match import Discrepancy, match_invoice_to_order

MAX_FAILED_PINS = 5
LOCK_FOR = timedelta(minutes=15)
_MIN_PIN_DIGITS = 6
_PAGE_DPI = 110
_QUEUE_LIMIT = 200
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
    """No extraction that can be reviewed (none yet, or made before review existed)."""


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
    signature_valid: bool | None  # None until signed

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

    @property
    def blockers(self) -> tuple[str, ...]:
        found = list(self.reassessed.assessment.reasons)
        if self.match_status == "mismatch":
            found.append("it does not match its purchase order")
        if self.document.doc_type == "invoice" and self.match_status == "no_counterpart":
            found.append("there is no purchase order on file to compare it with")
        return tuple(found)


def _field_paths(data: Any, prefix: str = "") -> list[str]:
    if isinstance(data, dict):
        if set(data) == {"text", "block_ids"}:
            return [prefix]
        return [
            p
            for key, value in data.items()
            for p in _field_paths(value, f"{prefix}.{key}" if prefix else key)
        ]
    if isinstance(data, list):
        return [
            p for index, item in enumerate(data) for p in _field_paths(item, f"{prefix}[{index}]")
        ]
    return []


def _now() -> datetime:
    return datetime.now(UTC)


class ReviewService:
    def __init__(
        self,
        sessions: SessionFactory,
        store: ObjectStore,
        specs: Mapping[str, DocumentSpec[Any]],
    ) -> None:
        self._sessions = sessions
        self._store = store
        self._specs = dict(specs)

    # Reviewers

    def add_reviewer(self, tenant_id: uuid.UUID, *, name: str, email: str, pin: str) -> uuid.UUID:
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
                tenant_id=tenant_id, name=name.strip(), email=email, pin_hash=hash_pin(pin)
            )
            session.add(reviewer)
            session.flush()
            return reviewer.id

    def _authenticate(self, tenant_id: uuid.UUID, email: str, pin: str) -> Reviewer:
        """The reviewer, if the PIN is right. A wrong PIN is counted, in its own transaction."""
        with self._sessions.begin() as session:
            reviewer = session.scalar(
                select(Reviewer)
                .where(Reviewer.tenant_id == tenant_id, Reviewer.email == email.strip().lower())
                .with_for_update()
            )
            if reviewer is None:
                verify_pin(pin, _DUMMY_HASH)
                raise NotAuthenticated
            now = _now()
            if reviewer.locked_until is not None and reviewer.locked_until > now:
                raise ReviewerLocked
            if not verify_pin(pin, reviewer.pin_hash):
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

    def queue(self, tenant_id: uuid.UUID) -> list[QueueItem]:
        """Documents whose newest extraction needs a person and has no signed review."""
        items: list[QueueItem] = []
        with self._sessions() as session:
            documents = session.scalars(
                select(Document)
                .where(Document.tenant_id == tenant_id, Document.status == "extracted")
                .order_by(Document.created_at)
                .limit(_QUEUE_LIMIT)
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
        return items

    def detail(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> ReviewDetail:
        with self._sessions() as session:
            return self._detail(self._state(session, self._find(session, tenant_id, document_id)))

    def page_image(self, tenant_id: uuid.UUID, document_id: uuid.UUID, page: int) -> bytes:
        """Page `page` of the original as uploaded, as PNG: what the reviewer compares with."""
        with self._sessions() as session:
            document = self._find(session, tenant_id, document_id)
            key, pages = document.storage_key, document.page_count or 0
        if not 1 <= page <= pages:
            raise LookupError(f"the document has no page {page}")
        (image,) = render_pages(self._store.get(key), _PAGE_DPI, page, page)
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()

    # Changing

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
    ) -> ReviewDetail:
        """Change (or confirm, by giving the same text) one field's printed value."""
        if not reason.strip():
            raise ValueError("a correction needs a reason")
        with self._sessions() as session:
            self._find(session, tenant_id, document_id)  # unknown documents before PIN checks
        reviewer = self._authenticate(tenant_id, email, pin)
        with self._sessions.begin() as session:
            document = self._find(session, tenant_id, document_id, lock=True)
            state = self._state(session, document)
            if state.review is not None:
                raise AlreadySigned
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

    def sign(
        self,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        outcome: str,
        meaning: str,
        reason: str,
        override_reason: str | None,
        email: str,
        pin: str,
    ) -> SignedReview:
        """Approve or reject the current record under the reviewer's signature."""
        if outcome not in ("approved", "rejected"):
            raise ValueError("outcome must be approved or rejected")
        if not meaning.strip() or not reason.strip():
            raise ValueError("a signature needs its meaning and a reason")
        override = override_reason.strip() if override_reason and override_reason.strip() else None
        with self._sessions() as session:
            self._find(session, tenant_id, document_id)
        reviewer = self._authenticate(tenant_id, email, pin)
        with self._sessions.begin() as session:
            document = self._find(session, tenant_id, document_id, lock=True)
            state = self._state(session, document)
            if state.review is not None:
                raise AlreadySigned
            blockers = state.blockers
            if outcome == "approved" and blockers and override is None:
                raise ApprovalBlocked(
                    "approval needs an override reason because " + "; ".join(blockers)
                )
            record = state.reassessed.extraction.model_dump(mode="json")
            digest = record_hash(record, outcome=outcome, meaning=meaning.strip())
            signed_at = _now()
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
                    data={"record": record, "draft": draft, "blockers": list(blockers)},
                    signed_at=signed_at,
                )
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

    def _state(self, session: Session, document: Document) -> _State:
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
            (correction, name)
            for correction, name in session.execute(
                select(CorrectionRow, Reviewer.name)
                .join(Reviewer, Reviewer.id == CorrectionRow.reviewer_id)
                .where(CorrectionRow.document_version_id == version.id)
                .order_by(CorrectionRow.created_at, CorrectionRow.id)
            ).tuples()
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
            other = session.scalar(
                select(Extraction).where(Extraction.document_version_id == other_id)
            )
            if other is not None:
                mine = reassessed.extraction
                if isinstance(mine, InvoiceExtraction):
                    found = match_invoice_to_order(
                        mine, PurchaseOrderExtraction.model_validate(other.data)
                    )
                else:
                    assert isinstance(mine, PurchaseOrderExtraction)  # noqa: S101
                    found = match_invoice_to_order(
                        InvoiceExtraction.model_validate(other.data), mine
                    )
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
        )

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
            stored = row.data.get("record")
            valid = (
                stored == record
                and record_hash(stored, outcome=row.outcome, meaning=row.meaning)
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
            editable_paths=tuple(_field_paths(state.raw.model_dump())),
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
        )


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
    "NotReviewable",
    "ReviewDetail",
    "ReviewService",
    "ReviewerLocked",
)
