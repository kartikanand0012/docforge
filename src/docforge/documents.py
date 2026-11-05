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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel
from sqlalchemy import case, exists, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from docforge import audit
from docforge.accounts import MemberCaps
from docforge.conversion import ConversionError, Converter, ConverterUnavailable, FileConverter
from docforge.db.models import (
    AssessmentRecord,
    AuditEntry,
    Document,
    DocumentSplit,
    DocumentVersion,
    EinvoiceCheck,
    Extraction,
    MatchRecord,
    ModelRun,
    ParseOutput,
    QuestionUsage,
    Review,
    Tenant,
    live,
)
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped, tenant_scope
from docforge.einvoice.check import EinvoiceChecker, QrCheck
from docforge.einvoice.view import EinvoiceView, einvoice_view, stored_check
from docforge.erasure import ANCHORS_PREFIX, Eraser, Erasure, live_copy, lock_content
from docforge.extraction.pipeline import (
    DEFAULT_MAX_PAGES,
    ExtractionError,
    PipelineResult,
    RulesFor,
)
from docforge.extraction.purchase_order import PurchaseOrderExtraction
from docforge.extraction.schema import InvoiceExtraction
from docforge.formats import ACCEPTED, FORMAT_OF, MEDIA_TYPES, Format, sniff
from docforge.llm.base import LLMError
from docforge.llm.replay import NotRecorded
from docforge.order_match import find_counterpart, record_match
from docforge.parsing.base import (
    DocumentTooLarge,
    NoTextLayer,
    ParsedDocument,
    ParseError,
    ParserLimitExceeded,
)
from docforge.parsing.cache import NotCached
from docforge.parsing.pdf import pdf_page_count
from docforge.review.claims import refuse_if_claimed
from docforge.split.detect import PagesDoNotReconcile, Proposal, reconcile
from docforge.split.pages import cut
from docforge.split.reader import Splitter
from docforge.stages import stage_listener
from docforge.storage import (
    ObjectNotFound,
    ObjectStore,
    StorageUnavailable,
    original_key,
    rendition_key,
)
from docforge.telemetry import Prices, current_prices, document_cost, traced
from docforge.trust.einvoice_rules import einvoice_rules
from docforge.trust.invoice_rules import InvoiceRule, near_expiry
from docforge.trust.rules import RuleResult
from docforge.workspace_settings import near_expiry_months

if TYPE_CHECKING:
    from docforge.anchors import AnchoredReport, AnchorStore

logger = logging.getLogger(__name__)

WORKER = "system:worker"
IN_FLIGHT = ("queued", "running")
# Document types that are compared with each other once both are extracted, joined on the
# order number each carries.
Outcome = Literal["succeeded", "failed", "skipped", "split"]


class UnknownDocumentType(ValueError):
    """No pipeline is registered for the document type."""


class UnsupportedFormat(ValueError):
    """The file is not one of the accepted formats (`docforge.formats`)."""


class DocumentTypeConflict(ValueError):
    """The same file is already stored under a different document type."""


class DocumentNotFound(LookupError):
    """No such document or version for this tenant."""


class ReprocessInProgress(Exception):
    """The document's newest version has not finished yet."""


PARSER_LIMIT = "Reading it was stopped: it needed more time or memory than one document may use."
NOT_RECORDED = "This demo reads only its recorded sample documents; this file was not recorded."


class QueueFull(Exception):
    """Too many documents are waiting to be processed."""


class WorkspaceFull(Exception):
    """The workspace holds as many documents as it may (a free workspace's cap)."""


# The audit actions that are a paid model reading: an upload, and a reading again.
READINGS = ("document.received", "document.reprocess_requested")


class DailyReadingsUsed(Exception):
    """The workspace has had as many readings today (uploads and readings again) as it may."""


class FreeReadingsUsed(Exception):
    """Free workspaces together have had as many readings today as the deployment allows."""


class SplitFile(Exception):
    """A file divided into its invoices (or waiting for a person to group its pages) is no
    bill itself: it is not read again or deleted while its invoices are kept."""


class SplitNotFound(LookupError):
    """No division of this document, or none waiting for a person."""


class SplitDecided(Exception):
    """The pages were grouped already: by the splitter, or by another person."""


class NoRoom(Exception):
    """A free workspace has no room for the invoices a divided file would make; in words."""


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
    def run(
        self, pdf: bytes, *, extra_rules_for: RulesFor | None = None
    ) -> PipelineResult[BaseModel]: ...


# Called inside the transaction that creates the version, so the job exists only if the
# version does. Raising aborts the whole ingest.
Enqueue = Callable[[Session, DocumentVersion], None]


@dataclass(frozen=True)
class IngestResult:
    document: Document
    version: DocumentVersion | None  # None when the file was already known
    created: bool


@dataclass(frozen=True)
class SplitGroupView:
    pages: tuple[int, ...]
    invoice_no: str | None  # as the splitter read it from the first page, when proposed
    seller_gstin: str | None
    document_id: uuid.UUID | None  # the invoice made from these pages, once made
    document_status: str | None
    new: bool  # made by this division (false: the same pages were a document already)


@dataclass(frozen=True)
class SplitView:
    """A file holding several invoices: what the splitter proposed, and what was made."""

    document_id: uuid.UUID
    filename: str
    status: str  # review: a person is to group the pages; split: the invoices are made
    automatic: bool
    reason: str | None  # why a person must confirm
    page_count: int
    proposed: tuple[SplitGroupView, ...]
    proposed_non_invoice: tuple[int, ...]
    pages_in_doubt: tuple[int, ...]
    groups: tuple[SplitGroupView, ...]  # as made; empty while in review
    non_invoice: tuple[int, ...]
    decided_by: str | None
    decided_person: str | None
    decided_at: datetime | None
    created_at: datetime


def _span(pages: Sequence[int]) -> str:
    if len(pages) == 1:
        return f"page {pages[0]}"
    if list(pages) == list(range(pages[0], pages[-1] + 1)):
        return f"pages {pages[0]}-{pages[-1]}"
    return "pages " + ", ".join(str(page) for page in pages)


def _proposal_json(proposal: Proposal) -> dict[str, Any]:
    return {
        "groups": [
            {"pages": list(g.pages), "invoice_no": g.invoice_no, "seller_gstin": g.seller_gstin}
            for g in proposal.groups
        ],
        "non_invoice": list(proposal.non_invoice),
        "pages_in_doubt": list(proposal.pages_in_doubt),
        "uncertain": list(proposal.uncertain),
    }


@dataclass(frozen=True)
class DocumentDetail:
    document: Document
    versions: list[DocumentVersion]


@dataclass(frozen=True)
class Usage:
    """What reading a document took, over every reading of it. Thinking tokens are billed
    as output, so they are counted in `output_tokens`."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model_cost_usd: float | None = None


@dataclass(frozen=True)
class Stats:
    """A workspace's figures, its live documents only."""

    documents: int
    ready: int  # read and finished (ready to ask, or processed)
    signed: int  # with a signed review of any version
    failed: int
    documents_last_7_days: int
    median_read_seconds: float | None  # from upload (or reprocess) to a finished reading
    model_cost_usd: float | None  # readings and questions; None when a model has no price


@dataclass(frozen=True)
class Step:
    """One step on a document's timeline: the stage reached, when, and why if it failed."""

    stage: str
    at: datetime
    detail: str | None = None


# Audit actions that move a document to a stage; `processing.stage` names its own.
_STAGE_OF = {
    "document.received": "stored",
    "document.reprocess_requested": "stored",
    "processing.started": "parsing",
    "processing.retry_scheduled": "retrying",
    "processing.failed": "failed",
    "indexing.completed": "ready",
    "split.proposed": "split_review",
    "split.made": "split",
    "split.confirmed": "split",
}


@dataclass(frozen=True)
class AssessmentDetail:
    version: DocumentVersion
    record: AssessmentRecord
    match: MatchRecord | None  # the newest comparison involving this version, if any
    counterpart_document_id: uuid.UUID | None
    doc_type: str
    einvoice: EinvoiceView | None = None  # for an invoice scanned as it was read

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
            # A deleted counterpart is no longer one.
            exists().where(Document.id == other.document_id, live()),
        )
        .order_by(MatchRecord.created_at.desc())
        .limit(1)
    ).first()
    return (found[0], found[1]) if found is not None else (None, None)


def _now() -> datetime:
    return datetime.now(UTC)


def _moved(proposal: dict[str, Any], groups: list[list[int]], non_invoice: list[int]) -> int:
    """Pages a person placed differently from the proposal: in another invoice, or moved
    into or out of "not an invoice page"."""

    def places(grouped: list[list[int]], outside: list[int]) -> dict[int, int]:
        # A group is known by its first page, so the order groups are listed in does not count.
        placed = {page: min(pages) for pages in grouped if pages for page in pages}
        placed.update({page: 0 for page in outside})
        return placed

    before = places(
        [g["pages"] for g in proposal.get("groups", [])], proposal.get("non_invoice", [])
    )
    after = places(groups, non_invoice)
    return sum(1 for page, place in after.items() if before.get(page) != place)


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
        index: Enqueue | None = None,
        converter: Converter | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        einvoice: EinvoiceChecker | None = None,
        splitter: Splitter | None = None,
        member_caps: MemberCaps | None = None,
    ) -> None:
        # Divides an invoice PDF holding several (V2 P6); None: every file is read whole.
        self._splitter = splitter
        # A free workspace's caps, held to the invoices a divided file makes as well.
        self._member_caps = member_caps or MemberCaps()
        self._einvoice = einvoice  # checks an invoice's signed QR; None: switched off
        self._max_pages = max_pages  # a PDF made from another file is held to it too
        self._converter = converter or FileConverter()  # files that are not PDFs, to PDFs
        self._index = index  # queues the search indexing of a finished version
        self._sessions = sessions
        self._events = events
        self._anchors = anchors
        self._store = store
        # Removes a deleted document's stored files, and a deleted workspace's.
        self.eraser = Eraser(
            sessions, store, anchors.prefix if anchors is not None else ANCHORS_PREFIX
        )
        self._pipelines = pipelines  # one per document type
        self._enqueue = enqueue
        self._max_attempts = max_attempts
        self._max_pending = max_pending

    @property
    def document_types(self) -> list[str]:
        return sorted(self._pipelines)

    def pages_allowed(self, doc_type: str, max_pages: int) -> int:
        """The pages an upload of `doc_type` may have: one invoice's, or with splitting on, a
        PDF holding several invoices (`Splitter.max_bundle_pages`)."""
        if self._splitter is not None and doc_type == "invoice":
            return max(max_pages, self._splitter.max_bundle_pages)
        return max_pages

    # --- ingest -----------------------------------------------------------------------------

    @scoped
    def ingest(
        self,
        *,
        tenant_id: uuid.UUID,
        doc_type: str,
        filename: str,
        data: bytes,
        actor: str,
        max_documents: int | None = None,
        readings_per_day: int | None = None,
        free_readings_per_day: int | None = None,
        source: str = "upload",
    ) -> IngestResult:
        """Record an upload (or a file mailed in: `source="email"`). The content hash is the
        identity: the same bytes for the same tenant, by any channel, return the existing
        document and start no new work.

        `max_documents`, `readings_per_day` and `free_readings_per_day`, when given, cap a
        new document: the live documents the tenant holds, its readings today (UTC: uploads
        and readings again, deleted documents' included), and every free workspace's.

        Raises `UnknownDocumentType`, `UnsupportedFormat`, `DocumentTypeConflict`,
        `QueueFull`, `WorkspaceFull`, `DailyReadingsUsed`, `FreeReadingsUsed` or
        `StorageUnavailable`. Nothing is recorded when it raises.
        """
        if doc_type not in self._pipelines:
            raise UnknownDocumentType(f"unknown document type {doc_type!r}")
        fmt = sniff(data)
        if fmt is None:
            raise UnsupportedFormat(f"not an accepted file type; accepted: {ACCEPTED}")
        media_type = MEDIA_TYPES[fmt]
        sha256 = hashlib.sha256(data).hexdigest()
        key = original_key(tenant_id, sha256, fmt)

        # Stored first, outside the transaction and its lock: an object without a row is
        # harmless and is reused by the next upload of the same bytes; a row without its
        # object is not. One stored here for an upload then refused is removed again.
        stored_now = False
        if not self._store.exists(key):
            self._store.put(key, data, media_type)
            stored_now = True
        try:
            return self._ingest(
                tenant_id, doc_type, filename, data, actor, sha256, key, media_type,
                max_documents, readings_per_day, free_readings_per_day, source,
            )  # fmt: skip
        except Exception:
            if stored_now:
                self._discard_unused(tenant_id, sha256, key)
            raise

    def _ingest(
        self,
        tenant_id: uuid.UUID,
        doc_type: str,
        filename: str,
        data: bytes,
        actor: str,
        sha256: str,
        key: str,
        media_type: str,
        max_documents: int | None,
        readings_per_day: int | None,
        free_readings_per_day: int | None,
        source: str = "upload",
    ) -> IngestResult:
        with self._sessions.begin() as session:
            # Not while these bytes' files are being erased (a document of them deleted).
            lock_content(session, tenant_id, sha256)
            existing = self._by_hash(session, tenant_id, sha256)
            inserted: uuid.UUID | None = None
            if existing is None:
                if self._pending(session, tenant_id) >= self._max_pending:
                    raise QueueFull("too many documents are waiting to be processed")
                self._within_caps(
                    session, tenant_id, max_documents, readings_per_day, free_readings_per_day
                )
                # Stored above, unless a deleted document's erasure removed it since.
                if not self._store.exists(key):
                    self._store.put(key, data, media_type)
                inserted = session.execute(
                    insert(Document)
                    .values(
                        tenant_id=tenant_id,
                        doc_type=doc_type,
                        sha256=sha256,
                        storage_key=key,
                        media_type=media_type,
                        filename=filename,
                        size_bytes=len(data),
                        source=source,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["tenant_id", "sha256"], index_where=live()
                    )
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
                    self._store.put(existing.storage_key, data, existing.media_type)
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
                media_type=media_type,
            )
            self._enqueue(session, version)
            return IngestResult(document, version, created=True)

    def _discard_unused(self, tenant_id: uuid.UUID, sha256: str, key: str) -> None:
        """Remove a file stored for an upload that was refused, unless a live document holds
        the same bytes by now (best effort: an object without a row is harmless)."""
        try:
            with self._sessions.begin() as session:
                lock_content(session, tenant_id, sha256)
                if not live_copy(session, tenant_id, sha256):
                    self._store.delete(key)
        except Exception:
            logger.warning("could not remove the unused object %s", key, exc_info=True)

    @scoped
    def reprocess(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        actor: str,
        reviewer_id: uuid.UUID | None = None,
        readings_per_day: int | None = None,
        free_readings_per_day: int | None = None,
    ) -> DocumentVersion:
        """Queue a new version of an existing document. Earlier versions are kept.

        Raises `ReprocessInProgress` while the newest version is queued or running, so a
        document has at most one version in flight, and `ClaimedByOther` while a reviewer
        other than `reviewer_id` (the signed-in person asking, if any) has it open. A reading
        again is a reading: `readings_per_day` and `free_readings_per_day` cap it as they
        cap an upload (`DailyReadingsUsed`, `FreeReadingsUsed`).
        """
        with self._sessions.begin() as session:
            document = self._document(session, tenant_id, document_id, lock=True)
            if document.status == "split":
                raise SplitFile("This file was divided into its invoices: read those again.")
            if document.status == "split_review":
                raise SplitFile("Group this file's pages into invoices first.")
            refuse_if_claimed(session, document.id, reviewer_id)
            newest = session.scalar(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.version_no.desc())
                .limit(1)
            )
            if newest is not None and newest.status in IN_FLIGHT:
                raise ReprocessInProgress(f"version {newest.version_no} is {newest.status}")
            self._within_caps(session, tenant_id, None, readings_per_day, free_readings_per_day)
            version_no = (newest.version_no if newest is not None else 0) + 1
            version = self._new_version(session, document, version_no=version_no)
            # Back to the start: not ready to chat with until the new version is indexed.
            document.status = "received"
            document.stage = "stored"
            self._audit(
                session, document, actor, "document.reprocess_requested", version_no=version_no
            )
            self._enqueue(session, version)
            return version

    @scoped
    def delete(
        self,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        actor: str,
        reviewer_id: uuid.UUID | None = None,
    ) -> None:
        """Delete a document: kept, with its history, but gone from everything a caller can
        read - lists, reads by id, search, questions, knowledge bases, exports and matching.

        Raises `DocumentNotFound` (also when already deleted) and `ClaimedByOther` while a
        reviewer other than `reviewer_id` has it open.
        """
        with self._sessions.begin() as session:
            document = self._document(session, tenant_id, document_id, lock=True)
            refuse_if_claimed(session, document.id, reviewer_id)
            children = self._children(session, document.id)
            if children:
                raise SplitFile(
                    f"This file was divided into {len(children)} invoices, which are kept from "
                    "it. Delete those first."
                )
            # A reading still waiting or under way ends here: the worker drops a deleted
            # document's job, and a version left in flight would hold a place in the queue.
            for version in session.scalars(
                select(DocumentVersion)
                .where(
                    DocumentVersion.document_id == document.id,
                    DocumentVersion.status.in_(IN_FLIGHT),
                )
                .with_for_update()
            ):
                version.status, version.error, version.finished_at = "failed", "Deleted", _now()
            # The application cannot mark it itself: once marked it cannot see the row. The
            # function also records that its stored files are to be erased.
            marked = session.execute(
                select(func.docforge_delete_document(document.id).table_valued(
                    "erasure_id", "erasure_token"
                ))
            ).first()  # fmt: skip
            if marked is None:
                raise DocumentNotFound(f"no document {document_id}")
            erasure_id, token = marked
            # No filename: this log can never be edited, and a filename may be personal data.
            self._audit(session, document, actor, "document.deleted")
            erasure = Erasure(
                erasure_id, "document", tenant_id, document.sha256, document.storage_key, token
            )
        # Its file and the PDFs made from it, once the deletion stands (best effort).
        self.eraser.erase_quietly(erasure)

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
        with traced("document.process") as span, tenant_scope(tenant_id):
            span.set_attribute("docforge.version_id", str(version_id))
            span.set_attribute("docforge.tenant_id", str(tenant_id))
            outcome = self._process(version_id)
            span.set_attribute("docforge.outcome", outcome)
            if outcome == "failed":
                span.set_status(Status(StatusCode.ERROR))
            return outcome

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
            fmt = FORMAT_OF[document.media_type]
            # A file that is not a PDF is made into one first.
            document.stage = "parsing" if fmt == "pdf" else "converting"
            self._audit(
                session,
                document,
                WORKER,
                "processing.started",
                version_no=version.version_no,
                attempt=version.attempts,
                stage=document.stage,
            )
            turn = version.attempts
            doc_type, storage_key, sha256 = document.doc_type, document.storage_key, document.sha256
            tenant = document.tenant_id
            # An invoice PDF may hold several; one cut from such a file holds one.
            divisible = (
                self._splitter is not None
                and doc_type == "invoice"
                and fmt == "pdf"
                and document.parent_document_id is None
            )
            # The workspace's near-expiry window as it is now, for the stored assessment.
            stock_rules = (
                (near_expiry(near_expiry_months(session, tenant)),) if doc_type == "invoice" else ()
            )

        final = turn >= self._max_attempts
        span = trace.get_current_span()
        span.set_attribute("docforge.doc_type", doc_type)
        span.set_attribute("docforge.attempt", turn)
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
            rendition: str | None = None
            if fmt != "pdf":
                # Kept under the converter's version, which may need asking (the service).
                rendition = rendition_key(tenant, sha256, self._converter.version)
                data = self._rendition(data, fmt, rendition)
                self._reach(version_id, turn, "parsing", recorded_at_start=False)
            # An invoice's signed QR, checked before it is read: its rules join the reading's.
            # The check never raises; whatever fails, it is `unreadable`.
            checked = (
                self._einvoice.check(data)
                if self._einvoice is not None and doc_type == "invoice"
                else None
            )
            evidence = checked.evidence if checked is not None else None

            def extra_rules_for(parsed: ParsedDocument) -> tuple[InvoiceRule, ...]:
                signed = einvoice_rules(evidence, parsed) if evidence is not None else ()
                return (*signed, *stock_rules)

            read: PipelineResult[BaseModel] | Proposal
            with stage_listener(lambda stage: self._reach(version_id, turn, stage)):
                pages = pdf_page_count(data) if divisible else 1
                if self._splitter is not None and pages > 1:
                    read = self._splitter.read(data, pages, extra_rules_for)
                else:
                    read = pipeline.run(data, extra_rules_for=extra_rules_for)
        except ConversionError as error:
            return self._fail(version_id, turn, f"The file could not be converted to PDF. {error}")
        except ConverterUnavailable as error:
            logger.warning("converter unavailable for version %s: %s", version_id, error)
            return self._retry_or_fail(
                version_id, turn, "The converter was unavailable.", error, final
            )
        except ObjectNotFound:
            return self._fail(version_id, turn, "The stored original is missing.")
        except NoTextLayer:
            return self._fail(version_id, turn, "No text could be read from the PDF.")
        except DocumentTooLarge as error:
            return self._fail(version_id, turn, f"The {error}.")
        except ParserLimitExceeded as error:
            # The limits stay in the log: shown, they help someone tune a file to sit under them.
            logger.warning("parser limit for version %s: %s", version_id, error)
            return self._fail(version_id, turn, PARSER_LIMIT)
        except (NotCached, NotRecorded):
            # A replaying deployment (the demo) has nothing recorded for this file.
            return self._fail(version_id, turn, NOT_RECORDED)
        except ParseError:
            message = (
                "The file could not be read as a PDF."
                if fmt == "pdf"
                else ("The PDF made from the file could not be read.")
            )
            return self._fail(version_id, turn, message)
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
        if isinstance(read, Proposal):
            return self._divide(version_id, turn, tenant, read, data, final)
        result = read
        calls = [
            (r.provider, r.model, r.input_tokens or 0, r.output_tokens or 0, r.thinking_tokens or 0)
            for r in result.responses
        ]
        span.set_attribute("docforge.model_calls", len(calls))
        span.set_attribute("docforge.input_tokens", sum(c[2] for c in calls))
        span.set_attribute("docforge.output_tokens", sum(c[3] + c[4] for c in calls))
        cost = document_cost(calls, current_prices())
        if cost is not None:
            span.set_attribute("docforge.cost_usd", cost)
        outcome = self._complete(version_id, turn, result, rendition, checked)
        if outcome == "succeeded" and rendition is not None:
            self._drop_superseded(version_id, tenant, sha256, rendition)
        if outcome == "succeeded":
            try:
                with traced("document.match"):
                    self._match(version_id)
            except Exception:
                # The extraction is stored; a failed comparison must not fail the job.
                logger.exception("could not match version %s with its counterpart", version_id)
        return outcome

    # --- one file holding several invoices (V2 P6) -------------------------------------------

    def _divide(
        self,
        version_id: uuid.UUID,
        turn: int,
        tenant: uuid.UUID,
        proposal: Proposal,
        data: bytes,
        final: bool,
    ) -> Outcome:
        """Record what the splitter found. When every page was certain the invoices are made
        now, each read as its own document; otherwise the file waits for a person to group
        its pages (Split Review) and nothing is made until they confirm."""
        groups = [list(group.pages) for group in proposal.groups]
        try:
            pdfs = [cut(data, pages) for pages in groups] if proposal.certain else []
            self._store_pdfs(tenant, pdfs)
        except ParseError:
            return self._fail(version_id, turn, "The file's pages could not be divided.")
        except StorageUnavailable as error:
            logger.warning("object storage unavailable for version %s: %s", version_id, error)
            return self._retry_or_fail(
                version_id, turn, "Object storage was unavailable.", error, final
            )
        with self._sessions.begin() as session:
            owned = self._owned(session, version_id, turn)
            if owned is None:
                return "skipped"
            document, version = owned
            version.status = "split"
            version.error = None
            version.finished_at = _now()
            document.page_count = proposal.page_count
            no_room = self._room_problem(session, document.tenant_id, len(groups))
            split = DocumentSplit(
                tenant_id=document.tenant_id, document_id=document.id, version_id=version.id,
                status="review", automatic=False, reason=proposal.reason or no_room,
                page_count=proposal.page_count, proposal=_proposal_json(proposal),
            )  # fmt: skip
            session.add(split)
            if proposal.certain and no_room is None:
                made = self._make_children(session, document, groups, pdfs, WORKER)
                self._made(session, document, split, groups, made, [], actor=WORKER)
            else:
                document.status = document.stage = "split_review"
                self._audit(
                    session, document, WORKER, "split.proposed",
                    pages=proposal.page_count, invoices=len(groups),
                    non_invoice_pages=len(proposal.non_invoice),
                    pages_in_doubt=len(proposal.pages_in_doubt),
                )  # fmt: skip
        return "split"

    def _store_pdfs(self, tenant: uuid.UUID, pdfs: Sequence[bytes]) -> None:
        """Stored before the transaction that records them, as an upload's file is: an
        object without a row is harmless."""
        for data in pdfs:
            key = original_key(tenant, hashlib.sha256(data).hexdigest(), "pdf")
            if not self._store.exists(key):
                self._store.put(key, data, "application/pdf")

    def _room_problem(self, session: Session, tenant_id: uuid.UUID, count: int) -> str | None:
        """Why a free workspace cannot take `count` new invoices now, in words; None if it
        can. An organisation is not held to a free workspace's caps."""
        tenant = session.get(Tenant, tenant_id)
        if tenant is None or tenant.kind != "personal":
            return None
        caps = self._member_caps
        session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"uploads:{tenant_id}"))))
        held = (
            session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.tenant_id == tenant_id, live())
            )
            or 0
        )
        if held + count > caps.max_documents:
            return (
                f"this free workspace holds up to {caps.max_documents} documents and has room "
                f"for {max(caps.max_documents - held, 0)}; the file holds {count} invoices"
            )
        today = func.date_trunc("day", func.now(), "UTC")
        readings = session.scalar(
            select(func.count()).select_from(AuditEntry).where(
                AuditEntry.tenant_id == tenant_id,
                AuditEntry.action.in_(READINGS),
                AuditEntry.occurred_at >= today,
            )
        ) or 0  # fmt: skip
        if readings + count > caps.uploads_per_day:
            return (
                f"this free workspace takes up to {caps.uploads_per_day} readings a day and has "
                f"{max(caps.uploads_per_day - readings, 0)} left today; the file holds {count} "
                "invoices"
            )
        return None

    def _make_children(
        self,
        session: Session,
        parent: Document,
        groups: list[list[int]],
        pdfs: list[bytes],
        actor: str,
    ) -> list[tuple[uuid.UUID, bool]]:
        """One invoice document per group, each queued to be read as any upload is. Pages
        already a document (the same bytes) are that document."""
        made: list[tuple[uuid.UUID, bool]] = []
        stem = (parent.filename.rsplit(".", 1)[0] or "file")[:150]
        for index, (pages, data) in enumerate(zip(groups, pdfs, strict=True), start=1):
            sha256 = hashlib.sha256(data).hexdigest()
            key = original_key(parent.tenant_id, sha256, "pdf")
            lock_content(session, parent.tenant_id, sha256)
            existing = self._by_hash(session, parent.tenant_id, sha256)
            if existing is not None:
                made.append((existing.id, False))
                continue
            if not self._store.exists(key):  # erased since it was stored: stored again
                self._store.put(key, data, "application/pdf")
            child = Document(
                tenant_id=parent.tenant_id, doc_type="invoice", sha256=sha256, storage_key=key,
                media_type="application/pdf", size_bytes=len(data), source=parent.source,
                filename=f"{stem} - invoice {index} of {len(groups)} ({_span(pages)}).pdf",
                parent_document_id=parent.id, parent_pages=pages,
            )  # fmt: skip
            session.add(child)
            session.flush()
            version = self._new_version(session, child, version_no=1)
            self._audit(
                session, child, actor, "document.received", sha256=sha256,
                size_bytes=len(data), doc_type="invoice", media_type="application/pdf",
                parent_document_id=str(parent.id), pages=len(pages),
            )  # fmt: skip
            self._enqueue(session, version)
            made.append((child.id, True))
        return made

    def _made(
        self,
        session: Session,
        parent: Document,
        split: DocumentSplit,
        groups: list[list[int]],
        made: list[tuple[uuid.UUID, bool]],
        non_invoice: list[int],
        *,
        actor: str,
        person: str | None = None,
        moved: int = 0,
    ) -> None:
        # Enforced here as well as where the grouping came from: no page is ever dropped.
        reconcile(split.page_count, groups, non_invoice)
        split.status = "split"
        split.automatic = person is None
        split.groups = [
            {"pages": pages, "document_id": str(document_id), "new": new}
            for pages, (document_id, new) in zip(groups, made, strict=True)
        ]
        split.non_invoice = non_invoice
        split.decided_by, split.decided_person, split.decided_at = actor, person, _now()
        parent.status = parent.stage = "split"
        details: dict[str, str | int] = {
            "pages": split.page_count,
            "invoices": len(groups),
            "invoice_pages": sum(len(pages) for pages in groups),
            "non_invoice_pages": len(non_invoice),
            "already_received": sum(1 for _, new in made if not new),
        }
        if person is None:
            self._audit(session, parent, actor, "split.made", **details)
        else:
            self._audit(
                session, parent, actor, "split.confirmed", person=person, pages_moved=moved,
                **details,
            )  # fmt: skip

    @scoped
    def confirm_split(
        self,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        groups: list[list[int]],
        non_invoice: list[int],
        *,
        actor: str,
        person: str,
    ) -> SplitView:
        """A person's grouping of a file's pages: one invoice per group, made and queued now.

        Raises `DocumentNotFound`, `SplitNotFound`, `SplitDecided`, `PagesDoNotReconcile`
        (with the reason in words) or `NoRoom`."""
        with self._sessions() as session:
            document = self._document(session, tenant_id, document_id)
            split = self._split_row(session, document.id)
            if split.status != "review":
                raise SplitDecided("The pages of this file were grouped already.")
            reconcile(split.page_count, groups, non_invoice)
            if not groups:
                raise PagesDoNotReconcile(
                    "Put at least one page in an invoice. If no page is an invoice, delete the "
                    "file instead."
                )
            longest = max(range(len(groups)), key=lambda i: len(groups[i]))
            limit = self._splitter.max_pages if self._splitter is not None else self._max_pages
            if len(groups[longest]) > limit:
                raise PagesDoNotReconcile(
                    f"Invoice {longest + 1} has {len(groups[longest])} pages; one invoice may "
                    f"have up to {limit}."
                )
            proposal = split.proposal
            key = document.storage_key
        groups = [sorted(pages) for pages in groups]
        non_invoice = sorted(non_invoice)
        data = self._store.get(key)
        pdfs = [cut(data, pages) for pages in groups]
        self._store_pdfs(tenant_id, pdfs)
        with self._sessions.begin() as session:
            document = self._document(session, tenant_id, document_id, lock=True)
            split = self._split_row(session, document.id, lock=True)
            if split.status != "review":
                raise SplitDecided("The pages of this file were grouped already.")
            no_room = self._room_problem(session, tenant_id, len(groups))
            if no_room is not None:
                raise NoRoom(no_room[0].upper() + no_room[1:] + ".")
            made = self._make_children(session, document, groups, pdfs, actor)
            self._made(
                session, document, split, groups, made, non_invoice, actor=actor, person=person,
                moved=_moved(proposal, groups, non_invoice),
            )  # fmt: skip
        return self.split_view(tenant_id, document_id)

    @staticmethod
    def _split_row(
        session: Session, document_id: uuid.UUID, *, lock: bool = False
    ) -> DocumentSplit:
        query = select(DocumentSplit).where(DocumentSplit.document_id == document_id)
        split = session.scalar(query.with_for_update() if lock else query)
        if split is None:
            raise SplitNotFound(f"no division of document {document_id}")
        return split

    @staticmethod
    def _children(session: Session, parent_id: uuid.UUID) -> list[Document]:
        return list(
            session.scalars(
                select(Document)
                .where(Document.parent_document_id == parent_id, live())
                .order_by(Document.created_at, Document.id)
            )
        )

    @scoped
    def split_view(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> SplitView:
        """Raises `DocumentNotFound` or `SplitNotFound`."""
        with self._sessions() as session:
            document = self._document(session, tenant_id, document_id)
            split = self._split_row(session, document.id)
            return self._split_views(session, [(split, document)])[0]

    @scoped
    def splits(
        self, tenant_id: uuid.UUID, *, status: str | None = None, limit: int = 50
    ) -> list[SplitView]:
        """Divided files, newest first; `status="review"`: those waiting for a person."""
        with self._sessions() as session:
            query = (
                select(DocumentSplit, Document)
                .join(Document, Document.id == DocumentSplit.document_id)
                .where(DocumentSplit.tenant_id == tenant_id, live())
                .order_by(DocumentSplit.created_at.desc(), DocumentSplit.id.desc())
                .limit(limit)
            )
            if status is not None:
                query = query.where(DocumentSplit.status == status)
            rows = [(split, document) for split, document in session.execute(query).all()]
            return self._split_views(session, rows)

    @staticmethod
    def _split_views(
        session: Session, rows: list[tuple[DocumentSplit, Document]]
    ) -> list[SplitView]:
        made_ids = {
            uuid.UUID(group["document_id"]) for split, _ in rows for group in (split.groups or [])
        }
        statuses = dict(
            session.execute(
                select(Document.id, Document.status).where(Document.id.in_(made_ids), live())
            ).all()
        ) if made_ids else {}  # fmt: skip
        views = []
        for split, document in rows:
            proposal = split.proposal
            proposed = tuple(
                SplitGroupView(tuple(g["pages"]), g.get("invoice_no"), g.get("seller_gstin"),
                               None, None, False)
                for g in proposal.get("groups", [])
            )  # fmt: skip
            groups = tuple(
                SplitGroupView(
                    tuple(g["pages"]), None, None, uuid.UUID(g["document_id"]),
                    statuses.get(uuid.UUID(g["document_id"]), "deleted"), bool(g["new"]),
                )
                for g in (split.groups or [])
            )  # fmt: skip
            views.append(
                SplitView(
                    document_id=document.id, filename=document.filename, status=split.status,
                    automatic=split.automatic, reason=split.reason, page_count=split.page_count,
                    proposed=proposed,
                    proposed_non_invoice=tuple(proposal.get("non_invoice", [])),
                    pages_in_doubt=tuple(proposal.get("pages_in_doubt", [])),
                    groups=groups, non_invoice=tuple(split.non_invoice or ()),
                    decided_by=split.decided_by, decided_person=split.decided_person,
                    decided_at=split.decided_at, created_at=split.created_at,
                )
            )  # fmt: skip
        return views

    def _drop_superseded(
        self, version_id: uuid.UUID, tenant: uuid.UUID, sha256: str, kept: str
    ) -> None:
        """Converted PDFs earlier readings used, now replaced: deleted, since the original
        can always be converted again. A failure here is logged; it costs only storage."""
        with self._sessions() as session:
            document_id = session.scalar(
                select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
            )
            used: set[str] = set(
                key
                for key in session.scalars(
                    select(DocumentVersion.rendition_key).where(
                        DocumentVersion.document_id == document_id,
                        DocumentVersion.rendition_key.is_not(None),
                    )
                )
                if key is not None
            )
        for key in (used | {rendition_key(tenant, sha256)}) - {kept}:
            try:
                if self._store.exists(key):
                    self._store.delete(key)
            except StorageUnavailable:
                logger.warning("could not delete a superseded converted PDF")

    def _rendition(self, data: bytes, fmt: Format, key: str) -> bytes:
        """The PDF made from `data`: stored the first time, read back after (reprocessing).

        Runs outside any transaction; the conversion can take seconds. One deleted between
        the check and the read (a newer reading replacing it) is made again.
        """
        stored = self._stored(key)
        if stored is not None:
            return stored
        with traced("document.convert") as span:
            span.set_attribute("docforge.format", fmt)
            pdf = self._converter.to_pdf(data, fmt)
        pages = pdf_page_count(pdf)
        if pages > self._max_pages:
            raise DocumentTooLarge(f"document has {pages} pages; the limit is {self._max_pages}")
        # LibreOffice's output differs run to run. If another delivery stored its PDF first,
        # that one is used, so page images and cited boxes always come from the same PDF.
        stored = self._stored(key)
        if stored is not None:
            return stored
        self._store.put(key, pdf, "application/pdf")
        return pdf

    def _stored(self, key: str) -> bytes | None:
        if not self._store.exists(key):
            return None
        try:
            return self._store.get(key)
        except ObjectNotFound:
            return None

    def _complete(
        self,
        version_id: uuid.UUID,
        turn: int,
        result: PipelineResult[BaseModel],
        rendition: str | None = None,
        checked: QrCheck | None = None,
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
            if checked is not None:
                # In this transaction: review rebuilds the e-invoice rules from this row, so it
                # exists if and only if the assessment made with them does.
                evidence = checked.evidence
                details: dict[str, str | int] = {
                    "version_no": version.version_no,
                    "status": checked.status,
                }
                if evidence is not None and evidence.irp:
                    details["irp"] = evidence.irp
                session.add(
                    EinvoiceCheck(
                        tenant_id=version.tenant_id,
                        document_version_id=version.id,
                        status=checked.status,
                        evidence=evidence.model_dump(mode="json") if evidence else None,
                        qr_text=checked.qr_text,
                        page=checked.page,
                        pages_scanned=checked.pages_scanned,
                        decoder=checked.decoder,
                    )
                )
                self._audit(
                    session,
                    document,
                    WORKER,
                    "einvoice.checked",
                    **details,
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
            model = result.responses[-1].model if result.responses else None  # general: none
            version.status = "succeeded"
            version.finished_at = _now()
            version.parser_version = f"{result.parsed.parser} {result.parsed.parser_version}"
            version.schema_version = result.schema_version
            version.prompt_version = result.prompt_version
            version.model_id = model
            version.rendition_key = rendition  # the PDF this reading was made from, if any
            document.status = "extracted"
            # Indexed for search and chat next, if this service queues that; else done here.
            document.stage = "indexing" if self._index is not None else "processed"
            document.page_count = len(result.parsed.pages)
            self._audit(
                session,
                document,
                WORKER,
                "extraction.created",
                version_no=version.version_no,
                extraction_sha256=sha256,
                model=model or "none",
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
            self._audit(session, document, WORKER, "processing.stage", stage=document.stage)
            if self._index is not None:
                self._index(session, version)
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
        sees the other. If both run at once, the unique pair keeps a single record. The
        counterpart is found by its values as corrected (`order_match`); the comparison stored
        here is of the two as read, and every review read compares them again as corrected.
        """
        with self._sessions.begin() as session:
            version = session.get_one(DocumentVersion, version_id)
            document = session.get(Document, version.document_id)
            if document is None or document.deleted_at is not None:
                return  # deleted meanwhile: nothing to compare
            found = find_counterpart(session, document, version.id)
            if found is None:
                return
            other_document, other_version = found
            stored = {
                row.document_version_id: row.data
                for row in session.scalars(
                    select(Extraction).where(
                        Extraction.document_version_id.in_((version.id, other_version))
                    )
                )
            }
            is_invoice = document.doc_type == "invoice"
            invoice = (document, version.id) if is_invoice else (other_document, other_version)
            order = (other_document, other_version) if is_invoice else (document, version.id)
            record_match(
                session,
                invoice=invoice,
                order=order,
                records=(
                    InvoiceExtraction.model_validate(stored[invoice[1]]),
                    PurchaseOrderExtraction.model_validate(stored[order[1]]),
                ),
                actor=WORKER,
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
            document.stage = "retrying"
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
        document.stage = "failed"
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
    def list_documents(
        self,
        tenant_id: uuid.UUID,
        *,
        limit: int = 50,
        before: tuple[datetime, uuid.UUID] | None = None,
        doc_type: str | None = None,
        stage: str | None = None,
        source: str | None = None,
    ) -> list[Document]:
        """The organisation's documents, newest first; `before` is the last one already seen
        (its created_at and id), so a page never repeats or skips one. `source`: upload or
        email."""
        query = select(Document).where(Document.tenant_id == tenant_id, live())
        if doc_type is not None:
            query = query.where(Document.doc_type == doc_type)
        if source is not None:
            query = query.where(Document.source == source)
        if stage is not None:
            query = query.where(Document.stage == stage)
        if before is not None:
            query = query.where(tuple_(Document.created_at, Document.id) < tuple_(*before))
        query = query.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit)
        with self._sessions() as session:
            return list(session.scalars(query))

    @scoped
    def timeline(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> list[Step]:
        """Every stage the document has reached, in order, from its audit trail."""
        steps = []
        for entry in self.audit_trail(tenant_id, document_id):
            stage = (
                entry.details.get("stage")
                if entry.action == "processing.stage"
                # Processing starts with parsing, or with converting a file that is not a PDF.
                else entry.details.get("stage", "parsing")
                if entry.action == "processing.started"
                else _STAGE_OF.get(entry.action)
            )
            if stage is not None:
                detail = entry.details.get("error") if stage in ("failed", "retrying") else None
                steps.append(Step(stage=stage, at=entry.occurred_at, detail=detail))
        return steps

    def mark_indexed(self, version_id: uuid.UUID) -> None:
        """Search has the version: the document is ready to chat with. Said once."""
        with self._sessions() as session:
            tenant_id = session.scalar(select(func.docforge_version_tenant(version_id)))
        if tenant_id is None:
            raise DocumentNotFound(version_id)
        with tenant_scope(tenant_id), self._sessions.begin() as session:
            document, version = self._locked(session, version_id)
            newest = session.scalar(
                select(func.max(DocumentVersion.version_no)).where(
                    DocumentVersion.document_id == document.id
                )
            )
            # Only the newest version, once it is waiting for its index. An index job of an
            # older version (retried late) must not make a document being reprocessed ready.
            if version.version_no != newest or document.stage != "indexing":
                return
            document.stage = "ready"
            self._audit(
                session, document, WORKER, "indexing.completed", version_no=version.version_no
            )
            if self._events is not None:
                self._events.emit(
                    session,
                    document.tenant_id,
                    "document.ready_for_chat",
                    {"document_id": str(document.id), "version_no": version.version_no},
                    event_id=event_id("document.ready_for_chat", version.id),
                )

    def _reach(
        self, version_id: uuid.UUID, turn: int, stage: str, *, recorded_at_start: bool = True
    ) -> None:
        """A stage the pipeline reported while working: recorded in its own short
        transaction, so the person waiting sees it at once. Only by the current delivery: a
        stalled one, overtaken by another, must not move a finished document back."""
        if stage == "parsing" and recorded_at_start:
            return  # recorded when processing started, unless conversion came first
        try:
            with self._sessions.begin() as session:
                owned = self._owned(session, version_id, turn)
                if owned is None:
                    return
                document, _ = owned
                document.stage = stage
                self._audit(session, document, WORKER, "processing.stage", stage=stage)
        except Exception:
            # Progress shown to a person; failing to show it must not redo the work.
            logger.warning("could not record stage %s of version %s", stage, version_id)

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
            rules = (RuleResult.model_validate(rule) for rule in record.data["rules"])
            einvoice = einvoice_view(stored_check(session, version.id), rules)
            return AssessmentDetail(
                version, record, match, counterpart, document.doc_type, einvoice
            )

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
            select(Document).where(
                Document.tenant_id == tenant_id, Document.sha256 == sha256, live()
            )
        )

    @scoped
    def usage(
        self, tenant_id: uuid.UUID, document_ids: Sequence[uuid.UUID], prices: Prices
    ) -> dict[uuid.UUID, Usage]:
        """Each document's model calls, tokens and cost; a document never read has none."""
        found: dict[uuid.UUID, list[tuple[str, str, int, int, int, int]]] = {}
        if document_ids:
            with self._sessions() as session:
                rows = session.execute(
                    select(
                        DocumentVersion.document_id, ModelRun.provider, ModelRun.model,
                        func.coalesce(func.sum(ModelRun.input_tokens), 0),
                        func.coalesce(func.sum(ModelRun.output_tokens), 0),
                        func.coalesce(func.sum(ModelRun.thinking_tokens), 0),
                        func.count(),
                    )
                    .join(DocumentVersion, DocumentVersion.id == ModelRun.document_version_id)
                    .where(
                        ModelRun.tenant_id == tenant_id,
                        DocumentVersion.document_id.in_(set(document_ids)),
                    )
                    .group_by(DocumentVersion.document_id, ModelRun.provider, ModelRun.model)
                ).all()  # fmt: skip
            for document_id, provider, model, i, o, t, n in rows:
                found.setdefault(document_id, []).append(
                    (provider, model, int(i or 0), int(o or 0), int(t or 0), int(n))
                )
        out = {}
        for document_id in document_ids:
            calls = found.get(document_id, [])
            out[document_id] = Usage(
                model_calls=sum(c[5] for c in calls),
                input_tokens=sum(c[2] for c in calls),
                output_tokens=sum(c[3] + c[4] for c in calls),
                model_cost_usd=document_cost([c[:5] for c in calls], prices),
            )
        return out

    @scoped
    def stats(self, tenant_id: uuid.UUID, prices: Prices) -> Stats:
        """The workspace's figures, deleted documents left out."""
        week = func.now() - text("interval '7 days'")
        finished = Document.stage.in_(("ready", "processed"))
        with self._sessions() as session:
            counts = session.execute(
                select(
                    func.count(),
                    func.count().filter(finished),
                    func.count().filter(Document.stage == "failed"),
                    func.count().filter(Document.created_at >= week),
                ).where(Document.tenant_id == tenant_id, live())
            ).one()
            signed = session.scalar(
                select(func.count(func.distinct(DocumentVersion.document_id)))
                .select_from(Review)
                .join(DocumentVersion, DocumentVersion.id == Review.document_version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Review.tenant_id == tenant_id, live())
            )
            median = session.scalar(
                select(
                    func.percentile_cont(0.5).within_group(
                        # Never below zero: the two times come from two clocks.
                        func.greatest(
                            0,
                            func.extract(
                                "epoch", DocumentVersion.finished_at - DocumentVersion.created_at
                            ),
                        )
                    )
                )
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentVersion.tenant_id == tenant_id,
                    DocumentVersion.status == "succeeded",
                    DocumentVersion.finished_at.is_not(None),
                    live(),
                )
            )
            readings = session.execute(
                select(
                    ModelRun.provider, ModelRun.model,
                    func.coalesce(func.sum(ModelRun.input_tokens), 0),
                    func.coalesce(func.sum(ModelRun.output_tokens), 0),
                    func.coalesce(func.sum(ModelRun.thinking_tokens), 0),
                )
                .join(DocumentVersion, DocumentVersion.id == ModelRun.document_version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(ModelRun.tenant_id == tenant_id, live())
                .group_by(ModelRun.provider, ModelRun.model)
            ).all()  # fmt: skip
            # Every answer's tokens, its conversation since deleted or not.
            questions = session.execute(
                select(
                    QuestionUsage.provider, QuestionUsage.model,
                    func.coalesce(func.sum(QuestionUsage.input_tokens), 0),
                    func.coalesce(func.sum(QuestionUsage.output_tokens), 0),
                )
                .where(QuestionUsage.tenant_id == tenant_id)
                .group_by(QuestionUsage.provider, QuestionUsage.model)
            ).all()  # fmt: skip
        usages = [(p, m, int(i or 0), int(o or 0), int(t or 0)) for p, m, i, o, t in readings]
        usages += [(str(p), str(m), int(i or 0), int(o or 0), 0) for p, m, i, o in questions]
        return Stats(
            documents=int(counts[0]),
            ready=int(counts[1]),
            signed=int(signed or 0),
            failed=int(counts[2]),
            documents_last_7_days=int(counts[3]),
            median_read_seconds=round(float(median), 3) if median is not None else None,
            model_cost_usd=document_cost(usages, prices),
        )

    @staticmethod
    def _within_caps(
        session: Session,
        tenant_id: uuid.UUID,
        max_documents: int | None,
        readings_per_day: int | None,
        free_readings_per_day: int | None,
    ) -> None:
        """Refuse a new document, or a reading again, over the caps. One at a time per
        tenant counts, so two arriving together cannot both take the last place.

        Every free workspace's readings are counted without a lock across workspaces: two
        arriving together in different workspaces may both take the last place, which a
        safety valve can allow."""
        if max_documents is None and readings_per_day is None and free_readings_per_day is None:
            return
        session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"uploads:{tenant_id}"))))
        if max_documents is not None:
            held = session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.tenant_id == tenant_id, live())
            )
            if (held or 0) >= max_documents:
                raise WorkspaceFull(max_documents)
        today = func.date_trunc("day", func.now(), "UTC")
        if readings_per_day is not None:
            # From the audit log, which keeps deleted documents' arrivals and readings too.
            readings = session.scalar(
                select(func.count())
                .select_from(AuditEntry)
                .where(
                    AuditEntry.tenant_id == tenant_id,
                    AuditEntry.action.in_(READINGS),
                    AuditEntry.occurred_at >= today,
                )
            )
            if (readings or 0) >= readings_per_day:
                raise DailyReadingsUsed(readings_per_day)
        if free_readings_per_day is not None:
            # Across every free workspace, which only the owner's function can count.
            everyone = session.scalar(select(func.docforge_member_readings_since(today)))
            if (everyone or 0) >= free_readings_per_day:
                raise FreeReadingsUsed(free_readings_per_day)

    @property
    def max_pending(self) -> int:
        return self._max_pending

    @scoped
    def pending(self, tenant_id: uuid.UUID) -> int:
        """Versions waiting or being read: the queue's places taken."""
        with self._sessions() as session:
            return self._pending(session, tenant_id)

    @staticmethod
    def _pending(session: Session, tenant_id: uuid.UUID) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(DocumentVersion)
                # A deleted document's version never holds a place, whatever its status.
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentVersion.tenant_id == tenant_id,
                    DocumentVersion.status.in_(IN_FLIGHT),
                    live(),
                )
            )
            or 0
        )

    @staticmethod
    def _document(
        session: Session, tenant_id: uuid.UUID, document_id: uuid.UUID, *, lock: bool = False
    ) -> Document:
        query = select(Document).where(
            Document.tenant_id == tenant_id, Document.id == document_id, live()
        )
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
            select(Document)
            .where(Document.id == document_id, live())
            .with_for_update(key_share=True)
        ).scalar_one_or_none()
        if document is None:  # deleted while it was being read: its work is dropped
            raise DocumentNotFound(f"no document {document_id}")
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
