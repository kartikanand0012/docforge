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
from typing import Literal, Protocol

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from docforge import audit
from docforge.db.models import (
    AuditEntry,
    Document,
    DocumentVersion,
    Extraction,
    ModelRun,
    ParseOutput,
)
from docforge.db.session import SessionFactory
from docforge.extraction.pipeline import ExtractionError, PipelineResult
from docforge.llm.base import LLMError
from docforge.parsing.base import DocumentTooLarge, NoTextLayer, ParseError
from docforge.storage import ObjectNotFound, ObjectStore, StorageUnavailable, original_key

logger = logging.getLogger(__name__)

WORKER = "system:worker"
IN_FLIGHT = ("queued", "running")
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
class ExtractionDetail:
    version: DocumentVersion
    extraction: Extraction
    model_runs: list[ModelRun]


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
    ) -> None:
        self._sessions = sessions
        self._store = store
        self._pipelines = pipelines  # one per document type
        self._enqueue = enqueue
        self._max_attempts = max_attempts
        self._max_pending = max_pending

    @property
    def document_types(self) -> list[str]:
        return sorted(self._pipelines)

    # --- ingest -----------------------------------------------------------------------------

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
            return self._fail(version_id, turn, "The PDF has no text layer.")
        except DocumentTooLarge as error:
            return self._fail(version_id, turn, f"The {error}.")
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
        return self._complete(version_id, turn, result)

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
        return "succeeded"

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

    def detail(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> DocumentDetail:
        with self._sessions() as session:
            document = self._document(session, tenant_id, document_id)
            versions = session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.version_no)
            )
            return DocumentDetail(document, list(versions))

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

    def verify_audit_chain(self, tenant_id: uuid.UUID) -> audit.ChainReport:
        with self._sessions() as session:
            return audit.verify_chain(session, tenant_id)

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
