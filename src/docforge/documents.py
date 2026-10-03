"""Documents: idempotent ingest, versioned processing, and reads.

Processing is at-least-once. A job may be delivered again after a crash, a retry, or a
worker that only looked dead, so:

- every delivery takes a turn number (the version's `attempts`) when it starts, and may only
  write its result while that is still the current turn. A delivery that was overtaken
  finds a newer turn and writes nothing;
- the result is written in one transaction, so an extraction is recorded once;
- `attempts` is also the budget: after `max_attempts` starts the version is failed, whether
  the earlier attempts ended in an error or in a dead worker.

Row locks are always taken in the order document, version, then the audit lock.
"""

import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from docforge import audit
from docforge.db.models import (
    ORDER_NUMBER,
    AssessmentRecord,
    AuditEntry,
    Document,
    DocumentVersion,
    Extraction,
    MatchRecord,
    ModelRun,
    ParseOutput,
)
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped, tenant_scope
from docforge.extraction.pipeline import ExtractionError, PipelineResult
from docforge.extraction.purchase_order import PurchaseOrderExtraction
from docforge.extraction.schema import InvoiceExtraction
from docforge.llm.base import LLMError
from docforge.parsing.base import (
    DocumentTooLarge,
    NoTextLayer,
    ParseError,
    ParserLimitExceeded,
)
from docforge.storage import ObjectNotFound, ObjectStore, StorageUnavailable, original_key
from docforge.trust.match import match_invoice_to_order

if TYPE_CHECKING:
    from docforge.anchors import AnchoredReport, AnchorStore

logger = logging.getLogger(__name__)

WORKER = "system:worker"
IN_FLIGHT = ("queued", "running")
# Document types that are compared with each other once both are extracted, joined on the
# order number each carries.
_COUNTERPART = {"invoice": "purchase_order", "purchase_order": "invoice"}
Outcome = Literal["succeeded", "failed", "skipped"]


class UnknownDocumentType(ValueError):
    """No pipeline is registered for the document type."""


class DocumentTypeConflict(ValueError):
    """The same file is already stored under a different document type."""


class DocumentNotFound(LookupError):
    """No such document or version for this tenant."""


class ReprocessInProgress(Exception):
    """The document's newest version has not finished yet."""


class QueueFull(Exception):
    """Too many documents are waiting to be processed."""


class TransientProcessingError(Exception):
    """Processing failed in a way that may succeed later; the queue should retry the job."""


class EventSink(Protocol):
    def emit(
        self,
        session: Session,
        tenant_id: uuid.UUID,
        event_type: str,
        data: dict[str, Any],
        *,
        event_id: uuid.UUID | None = None,
    ) -> uuid.UUID: ...


_EVENTS = uuid.UUID("6f1c3c0e-8a51-4c3a-9a4f-2f0d9d6b7c11")


def event_id(event_type: str, subject: uuid.UUID) -> uuid.UUID:
    """The same event for the same subject always has the same id, so a job delivered twice
    emits it once."""
    return uuid.uuid5(_EVENTS, f"{event_type}:{subject}")


class Pipeline(Protocol):
    def run(self, pdf: bytes) -> PipelineResult[BaseModel]: ...


# Called inside the transaction that creates the version, so the job exists only if the
# version does. Raising aborts the whole ingest.
Enqueue = Callable[[Session, DocumentVersion], None]


@dataclass(frozen=True)
class IngestResult:
    document: Document
    version: DocumentVersion | None  # None when the file was already known
    created: bool


@dataclass(frozen=True)
class DocumentDetail:
    document: Document
    versions: list[DocumentVersion]


@dataclass(frozen=True)
class AssessmentDetail:
    version: DocumentVersion
    record: AssessmentRecord
    match: MatchRecord | None  # the newest comparison involving this version, if any
    counterpart_document_id: uuid.UUID | None
    doc_type: str

    @property
    def match_status(self) -> str:
        """`match`, `mismatch`, or `no_counterpart` when nothing is on file to compare with."""
        return "no_counterpart" if self.match is None else self.match.decision

    @property
    def decision(self) -> str:
        """`review` if the document's own checks or its match say so.

        An invoice with no order on file is also `review`: its own checks can pass on a
        forged but self-consistent document, so nothing independent supports it yet.
        """
        unsupported = self.doc_type == "invoice" and self.match is None
        mismatch = self.match_status == "mismatch"
        own = self.record.decision == "review"
        return "review" if own or mismatch or unsupported else "accept"


@dataclass(frozen=True)
class ExtractionDetail:
    version: DocumentVersion
    extraction: Extraction
    model_runs: list[ModelRun]


def current_match(
    session: Session, tenant_id: uuid.UUID, version_id: uuid.UUID
) -> tuple[MatchRecord | None, uuid.UUID | None]:
    """The newest comparison of this version, and the counterpart's document id.

    A comparison counts only while the other side is still that document's newest version:
    once the counterpart is read again, the old comparison is history.
    """
    other = aliased(DocumentVersion)
    newer = aliased(DocumentVersion)
    found = session.execute(
        select(MatchRecord, other.document_id)
        .join(
            other,
            other.id
            == case(
                (MatchRecord.invoice_version_id == version_id, MatchRecord.order_version_id),
                else_=MatchRecord.invoice_version_id,
            ),
        )
        .where(
            MatchRecord.tenant_id == tenant_id,
            (MatchRecord.invoice_version_id == version_id)
            | (MatchRecord.order_version_id == version_id),
            other.version_no
            == select(func.max(newer.version_no))
            .where(newer.document_id == other.document_id)
            .scalar_subquery(),
        )
        .order_by(MatchRecord.created_at.desc())
        .limit(1)
    ).first()
    return (found[0], found[1]) if found is not None else (None, None)


_MATCH_CANDIDATES = 20


def _parties(data: Mapping[str, Any], *, invoice: bool) -> tuple[object, object]:
    """(supplier GSTIN, buyer GSTIN) from a stored invoice or purchase-order extraction."""
    supplier = (data.get("seller") or {}).get("gstin") if invoice else data.get("supplier_gstin")
    buyer = (data.get("buyer") or {}).get("gstin")
    return (supplier or {}).get("value") or None, (buyer or {}).get("value") or None


def _now() -> datetime:
    return datetime.now(UTC)


class DocumentService:
    def __init__(
        self,
        sessions: SessionFactory,
        store: ObjectStore,
        pipelines: Mapping[str, Pipeline],
        enqueue: Enqueue,
        *,
        max_attempts: int = 5,
        max_pending: int = 1000,
        events: EventSink | None = None,
        anchors: "AnchorStore | None" = None,
    ) -> None:
        self._sessions = sessions
        self._events = events
        self._anchors = anchors
        self._store = store
        self._pipelines = pipelines  # one per document type
        self._enqueue = enqueue
        self._max_attempts = max_attempts
        self._max_pending = max_pending

    @property
    def document_types(self) -> list[str]:
        return sorted(self._pipelines)

    # --- ingest -----------------------------------------------------------------------------

    @scoped
    def ingest(
        self, *, tenant_id: uuid.UUID, doc_type: str, filename: str, data: bytes, actor: str
    ) -> IngestResult:
        """Record an upload. The content hash is the identity: the same bytes for the same
        tenant return the existing document and start no new work.

        Raises `UnknownDocumentType`, `DocumentTypeConflict`, `QueueFull` or
        `StorageUnavailable`. Nothing is recorded when it raises.
        """
        if doc_type not in self._pipelines:
            raise UnknownDocumentType(f"unknown document type {doc_type!r}")
        sha256 = hashlib.sha256(data).hexdigest()
        key = original_key(tenant_id, sha256)

        with self._sessions.begin() as session:
            existing = self._by_hash(session, tenant_id, sha256)
            inserted: uuid.UUID | None = None
            if existing is None:
                if self._pending(session, tenant_id) >= self._max_pending:
                    raise QueueFull("too many documents are waiting to be processed")
                # Store before the row: an object without a row is harmless and is reused
                # by the next upload of the same bytes; a row without its object is not.
                if not self._store.exists(key):
                    self._store.put(key, data, "application/pdf")
                inserted = session.execute(
                    insert(Document)
                    .values(
                        tenant_id=tenant_id,
                        doc_type=doc_type,
                        sha256=sha256,
                        storage_key=key,
                        filename=filename,
                        size_bytes=len(data),
                    )
                    .on_conflict_do_nothing(index_elements=["tenant_id", "sha256"])
                    .returning(Document.id)
                ).scalar_one_or_none()
                if inserted is None:  # a concurrent upload of the same bytes won
                    existing = self._by_hash(session, tenant_id, sha256)

            if existing is not None:
                if existing.doc_type != doc_type:
                    raise DocumentTypeConflict(
                        f"this file is already stored as document type {existing.doc_type!r}"
                    )
                if not self._store.exists(existing.storage_key):  # heal a lost original
                    self._store.put(existing.storage_key, data, "application/pdf")
                return IngestResult(existing, None, created=False)

            document = session.get_one(Document, inserted)
            version = self._new_version(session, document, version_no=1)
            # The filename is deliberately left out: this log can never be edited, and a
            # filename may hold personal data that has to be erasable.
            self._audit(
                session,
                document,
                actor,
                "document.received",
                sha256=sha256,
                size_bytes=len(data),
                doc_type=doc_type,
            )
            self._enqueue(session, version)
            return IngestResult(document, version, created=True)

    @scoped
    def reprocess(
        self, *, tenant_id: uuid.UUID, document_id: uuid.UUID, actor: str
    ) -> DocumentVersion:
        """Queue a new version of an existing document. Earlier versions are kept.

        Raises `ReprocessInProgress` while the newest version is queued or running, so a
        document has at most one version in flight.
        """
        with self._sessions.begin() as session:
            document = self._document(session, tenant_id, document_id, lock=True)
            newest = session.scalar(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.version_no.desc())
                .limit(1)
            )
            if newest is not None and newest.status in IN_FLIGHT:
                raise ReprocessInProgress(f"version {newest.version_no} is {newest.status}")
            version_no = (newest.version_no if newest is not None else 0) + 1
            version = self._new_version(session, document, version_no=version_no)
            self._audit(
                session, document, actor, "document.reprocess_requested", version_no=version_no
            )
            self._enqueue(session, version)
            return version

    # --- processing -------------------------------------------------------------------------

    def process(self, version_id: uuid.UUID) -> Outcome:
        """Run one delivery of a version's job. Safe to call any number of times.

        Raises `TransientProcessingError` when the queue should deliver the job again.
        """
        # The job carries only the version; its tenant is looked up before anything is read.
        with self._sessions() as session:
            tenant_id = session.scalar(select(func.docforge_version_tenant(version_id)))
        if tenant_id is None:
            raise DocumentNotFound(version_id)
        with tenant_scope(tenant_id):
            return self._process(version_id)

    def _process(self, version_id: uuid.UUID) -> Outcome:
        with self._sessions.begin() as session:
            document, version = self._locked(session, version_id)
            if version.status not in IN_FLIGHT:
                return "skipped"
            if version.attempts >= self._max_attempts:
                # Every earlier attempt started and none reported back: the worker died
                # each time. Stop here instead of killing workers forever.
                self._mark_failed(
                    session, document, version, "Processing was interrupted too many times."
                )
                return "failed"
            version.status = "running"
            version.attempts += 1
            version.started_at = version.started_at or _now()
            version.error = None
            document.status = "processing"
            self._audit(
                session,
                document,
                WORKER,
                "processing.started",
                version_no=version.version_no,
                attempt=version.attempts,
            )
            turn = version.attempts
            doc_type, storage_key, sha256 = document.doc_type, document.storage_key, document.sha256

        final = turn >= self._max_attempts
        pipeline = self._pipelines.get(doc_type)
        if pipeline is None:
            return self._fail(version_id, turn, "No pipeline is registered for this document type.")

        # The slow part runs outside any transaction.
        try:
            data = self._store.get(storage_key)
            if hashlib.sha256(data).hexdigest() != sha256:
                return self._fail(
                    version_id, turn, "The stored original does not match its recorded hash."
                )
            result = pipeline.run(data)
        except ObjectNotFound:
            return self._fail(version_id, turn, "The stored original is missing.")
        except NoTextLayer:
            return self._fail(version_id, turn, "No text could be read from the PDF.")
        except DocumentTooLarge as error:
            return self._fail(version_id, turn, f"The {error}.")
        except ParserLimitExceeded as error:
            return self._fail(version_id, turn, str(error))
        except ParseError:
            return self._fail(version_id, turn, "The file could not be read as a PDF.")
        except ExtractionError:
            return self._fail(version_id, turn, "The model reply did not fit the schema.")
        except StorageUnavailable as error:
            logger.warning("object storage unavailable for version %s: %s", version_id, error)
            return self._retry_or_fail(
                version_id, turn, "Object storage was unavailable.", error, final
            )
        except LLMError as error:
            logger.warning("model provider failed for version %s: %s", version_id, error)
            return self._retry_or_fail(version_id, turn, "The model provider failed.", error, final)
        except Exception as error:
            logger.exception("unexpected error processing version %s", version_id)
            return self._retry_or_fail(version_id, turn, "Internal error.", error, final)
        outcome = self._complete(version_id, turn, result)
        if outcome == "succeeded":
            try:
                self._match(version_id)
            except Exception:
                # The extraction is stored; a failed comparison must not fail the job.
                logger.exception("could not match version %s with its counterpart", version_id)
        return outcome

    def _complete(
        self, version_id: uuid.UUID, turn: int, result: PipelineResult[BaseModel]
    ) -> Outcome:
        data = result.extraction.model_dump(mode="json")
        sha256 = hashlib.sha256(audit.canonical_json(data).encode("utf-8")).hexdigest()
        with self._sessions.begin() as session:
            owned = self._owned(session, version_id, turn)
            if owned is None:
                return "skipped"
            document, version = owned
            session.add(
                ParseOutput(
                    tenant_id=version.tenant_id,
                    document_version_id=version.id,
                    data=result.parsed.model_dump(mode="json"),
                )
            )
            session.add(
                Extraction(
                    tenant_id=version.tenant_id,
                    document_version_id=version.id,
                    schema_version=result.schema_version,
                    data=data,
                    sha256=sha256,
                    raw=result.raw.model_dump(mode="json"),
                )
            )
            for call_no, response in enumerate(result.responses, start=1):
                session.add(
                    ModelRun(
                        tenant_id=version.tenant_id,
                        document_version_id=version.id,
                        call_no=call_no,
                        provider=response.provider,
                        model=response.model,
                        prompt_version=result.prompt_version,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        thinking_tokens=response.thinking_tokens,
                        latency_ms=response.latency_ms,
                    )
                )
            assessment = result.assessment
            session.add(
                AssessmentRecord(
                    tenant_id=version.tenant_id,
                    document_version_id=version.id,
                    decision=assessment.decision,
                    data=assessment.model_dump(mode="json"),
                )
            )
            model = result.responses[-1].model
            version.status = "succeeded"
            version.finished_at = _now()
            version.parser_version = f"{result.parsed.parser} {result.parsed.parser_version}"
            version.schema_version = result.schema_version
            version.prompt_version = result.prompt_version
            version.model_id = model
            document.status = "extracted"
            document.page_count = len(result.parsed.pages)
            self._audit(
                session,
                document,
                WORKER,
                "extraction.created",
                version_no=version.version_no,
                extraction_sha256=sha256,
                model=model,
                model_calls=len(result.responses),
            )
            self._audit(
                session,
                document,
                WORKER,
                "assessment.created",
                version_no=version.version_no,
                decision=assessment.decision,
                values_flagged=sum(field.needs_review for field in assessment.fields),
                checks_failed=sum(
                    rule.outcome == "failed" and rule.severity == "error"
                    for rule in assessment.rules
                ),
            )
            if self._events is not None:
                # In this transaction: the event exists if and only if the extraction does.
                self._events.emit(
                    session,
                    document.tenant_id,
                    "document.processed",
                    {
                        "document_id": str(document.id),
                        "doc_type": document.doc_type,
                        "version_no": version.version_no,
                        "decision": assessment.decision,
                        "reasons": list(assessment.reasons),
                    },
                    event_id=event_id("document.processed", version.id),
                )
        return "succeeded"

    def _match(self, version_id: uuid.UUID) -> None:
        """Compare a newly extracted invoice or order with its counterpart, if one is stored.

        Runs after the extraction has committed, so whichever of the two finishes second
        sees the other. If both run at once, the unique pair keeps a single record.
        """
        with self._sessions.begin() as session:
            version = session.get_one(DocumentVersion, version_id)
            document = session.get_one(Document, version.document_id)
            other_type = _COUNTERPART.get(document.doc_type)
            mine = session.scalar(
                select(Extraction).where(Extraction.document_version_id == version.id)
            )
            if other_type is None or mine is None:
                return
            order_number = (mine.data.get("po_no") or {}).get("value")
            if order_number is None:
                return
            # Only a document's newest version is a candidate, never a superseded one.
            newer = aliased(DocumentVersion)
            newest = (
                select(func.max(newer.version_no))
                .where(newer.document_id == Document.id)
                .correlate(Document)
                .scalar_subquery()
            )
            candidates = session.execute(
                select(DocumentVersion, Extraction, Document)
                .join(Extraction, Extraction.document_version_id == DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    Document.tenant_id == document.tenant_id,
                    Document.doc_type == other_type,
                    DocumentVersion.version_no == newest,
                    order_number == ORDER_NUMBER,
                )
                .order_by(DocumentVersion.created_at.desc())
                .limit(_MATCH_CANDIDATES)
            ).all()
            is_invoice = document.doc_type == "invoice"
            # The order number alone is whatever the document says. The two must also name
            # the same supplier and buyer before they are treated as a pair.
            parties = _parties(mine.data, invoice=is_invoice)
            found = next(
                (
                    row
                    for row in candidates
                    if None not in parties
                    and _parties(row[1].data, invoice=not is_invoice) == parties
                ),
                None,
            )
            if found is None:
                return
            other_version, theirs, other_document = found
            invoice_side = (version, mine) if is_invoice else (other_version, theirs)
            order_side = (other_version, theirs) if is_invoice else (version, mine)
            discrepancies = match_invoice_to_order(
                InvoiceExtraction.model_validate(invoice_side[1].data),
                PurchaseOrderExtraction.model_validate(order_side[1].data),
            )
            decision = "mismatch" if any(d.severity == "error" for d in discrepancies) else "match"
            inserted = session.execute(
                insert(MatchRecord)
                .values(
                    tenant_id=document.tenant_id,
                    invoice_version_id=invoice_side[0].id,
                    order_version_id=order_side[0].id,
                    decision=decision,
                    data={"discrepancies": [d.model_dump(mode="json") for d in discrepancies]},
                )
                .on_conflict_do_nothing(index_elements=["invoice_version_id", "order_version_id"])
                .returning(MatchRecord.id)
            ).scalar_one_or_none()
            if inserted is None:
                return
            for subject, counterpart in ((document, other_document), (other_document, document)):
                self._audit(
                    session,
                    subject,
                    WORKER,
                    "match.created",
                    counterpart_document_id=str(counterpart.id),
                    decision=decision,
                    discrepancies=len(discrepancies),
                )

    def _fail(self, version_id: uuid.UUID, turn: int, message: str) -> Outcome:
        """A failure that retrying will not fix. `message` is safe to show to a client."""
        with self._sessions.begin() as session:
            owned = self._owned(session, version_id, turn)
            if owned is None:
                return "skipped"
            self._mark_failed(session, *owned, message)
        return "failed"

    def _retry_or_fail(
        self, version_id: uuid.UUID, turn: int, message: str, error: Exception, final: bool
    ) -> Outcome:
        if final:
            return self._fail(version_id, turn, message)
        with self._sessions.begin() as session:
            owned = self._owned(session, version_id, turn)
            if owned is None:
                return "skipped"
            document, version = owned
            version.status = "queued"
            version.error = message
            self._audit(
                session,
                document,
                WORKER,
                "processing.retry_scheduled",
                version_no=version.version_no,
                attempt=version.attempts,
                error=message,
            )
        raise TransientProcessingError(message) from error

    def _mark_failed(
        self, session: Session, document: Document, version: DocumentVersion, message: str
    ) -> None:
        version.status = "failed"
        version.error = message
        version.finished_at = _now()
        # The document's status is that of its newest version; an earlier extraction, if
        # any, is still served by `latest_extraction`.
        document.status = "failed"
        self._audit(
            session,
            document,
            WORKER,
            "processing.failed",
            version_no=version.version_no,
            error=message,
        )

    # --- reads ------------------------------------------------------------------------------

    @scoped
    def detail(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> DocumentDetail:
        with self._sessions() as session:
            document = self._document(session, tenant_id, document_id)
            versions = session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.version_no)
            )
            return DocumentDetail(document, list(versions))

    @scoped
    def latest_extraction(
        self, tenant_id: uuid.UUID, document_id: uuid.UUID
    ) -> ExtractionDetail | None:
        """The extraction of the newest version that succeeded, if any."""
        with self._sessions() as session:
            document = self._document(session, tenant_id, document_id)
            row = session.execute(
                select(DocumentVersion, Extraction)
                .join(Extraction, Extraction.document_version_id == DocumentVersion.id)
                .where(
                    DocumentVersion.document_id == document.id,
                    Extraction.tenant_id == tenant_id,
                )
                .order_by(DocumentVersion.version_no.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            version, extraction = row
            runs = session.scalars(
                select(ModelRun)
                .where(ModelRun.document_version_id == version.id, ModelRun.tenant_id == tenant_id)
                .order_by(ModelRun.call_no)
            )
            return ExtractionDetail(version, extraction, list(runs))

    @scoped
    def assessment(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> AssessmentDetail | None:
        """The checks on the newest version that succeeded, with its match if there is one."""
        with self._sessions() as session:
            document = self._document(session, tenant_id, document_id)
            row = session.execute(
                select(DocumentVersion, AssessmentRecord)
                .join(AssessmentRecord, AssessmentRecord.document_version_id == DocumentVersion.id)
                .where(
                    DocumentVersion.document_id == document.id,
                    AssessmentRecord.tenant_id == tenant_id,
                )
                .order_by(DocumentVersion.version_no.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            version, record = row
            match, counterpart = current_match(session, tenant_id, version.id)
            return AssessmentDetail(version, record, match, counterpart, document.doc_type)

    @scoped
    def audit_trail(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> list[AuditEntry]:
        with self._sessions() as session:
            document = self._document(session, tenant_id, document_id)
            entries = session.scalars(
                select(AuditEntry)
                .where(
                    AuditEntry.tenant_id == tenant_id,
                    AuditEntry.target_type == "document",
                    AuditEntry.target_id == str(document.id),
                )
                .order_by(AuditEntry.id)
            )
            return list(entries)

    @scoped
    def verify_audit_chain(self, tenant_id: uuid.UUID) -> "AnchoredReport":
        """The chain, and, where anchors are kept, the chain against its latest anchor."""
        from docforge.anchors import AnchoredReport, verify_with_anchors

        with self._sessions() as session:
            if self._anchors is not None:
                return verify_with_anchors(session, tenant_id, self._anchors)
            chain = audit.verify_chain(session, tenant_id)
            return AnchoredReport(
                chain.consistent, chain.entries, chain.first_bad_id, chain.reason, 0
            )

    # --- helpers ----------------------------------------------------------------------------

    @staticmethod
    def _by_hash(session: Session, tenant_id: uuid.UUID, sha256: str) -> Document | None:
        return session.scalar(
            select(Document).where(Document.tenant_id == tenant_id, Document.sha256 == sha256)
        )

    @staticmethod
    def _pending(session: Session, tenant_id: uuid.UUID) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(DocumentVersion)
                .where(
                    DocumentVersion.tenant_id == tenant_id, DocumentVersion.status.in_(IN_FLIGHT)
                )
            )
            or 0
        )

    @staticmethod
    def _document(
        session: Session, tenant_id: uuid.UUID, document_id: uuid.UUID, *, lock: bool = False
    ) -> Document:
        query = select(Document).where(Document.tenant_id == tenant_id, Document.id == document_id)
        # FOR NO KEY UPDATE: enough to serialise writers without blocking new child rows.
        document = session.scalar(query.with_for_update(key_share=True) if lock else query)
        if document is None:
            raise DocumentNotFound(f"no document {document_id}")
        return document

    @staticmethod
    def _locked(session: Session, version_id: uuid.UUID) -> tuple[Document, DocumentVersion]:
        """The version and its document, both locked: the document first, then the version."""
        document_id = session.scalar(
            select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
        )
        if document_id is None:
            raise DocumentNotFound(f"no version {version_id}")
        document = session.execute(
            select(Document).where(Document.id == document_id).with_for_update(key_share=True)
        ).scalar_one()
        version = session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.id == version_id)
            .with_for_update(key_share=True)
        ).scalar_one()
        return document, version

    def _owned(
        self, session: Session, version_id: uuid.UUID, turn: int
    ) -> tuple[Document, DocumentVersion] | None:
        """The locked rows if delivery `turn` is still the current one, else None."""
        document, version = self._locked(session, version_id)
        if version.status != "running" or version.attempts != turn:
            logger.info(
                "delivery %s of version %s was overtaken; discarding its result", turn, version_id
            )
            return None
        return document, version

    @staticmethod
    def _new_version(session: Session, document: Document, version_no: int) -> DocumentVersion:
        version = DocumentVersion(
            tenant_id=document.tenant_id, document_id=document.id, version_no=version_no
        )
        session.add(version)
        session.flush()
        session.refresh(version)  # load the server defaults (status, attempts, created_at)
        return version

    @staticmethod
    def _audit(
        session: Session, document: Document, actor: str, action: str, **details: str | int
    ) -> None:
        audit.append(
            session,
            tenant_id=document.tenant_id,
            actor=actor,
            action=action,
            target_type="document",
            target_id=str(document.id),
            details=dict(details),
        )
