"""Ingest and processing against a real database, with a stand-in parser, model and store."""

import hashlib
import json
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docforge import audit
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import (
    AuditEntry,
    Document,
    DocumentVersion,
    Extraction,
    ModelRun,
    ParseOutput,
)
from docforge.db.session import SessionFactory
from docforge.documents import (
    DocumentNotFound,
    DocumentService,
    DocumentTypeConflict,
    QueueFull,
    ReprocessInProgress,
    TransientProcessingError,
    UnknownDocumentType,
)
from docforge.extraction.pipeline import InvoicePipeline, PipelineResult
from docforge.llm.base import LLMError
from docforge.parsing.base import ParsedDocument
from docforge.storage import MemoryObjectStore, original_key
from fakes import PARSED, FakeParser, ScriptedProvider

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PDF = (FIXTURES / "pair_001" / "invoice.pdf").read_bytes()
OTHER_PDF = (FIXTURES / "pair_002" / "invoice.pdf").read_bytes()
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))
SHA = hashlib.sha256(PDF).hexdigest()

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


class Killed(BaseException):
    """Stands in for a worker process dying: nothing gets a chance to handle it."""


class HookedPipeline:
    """Runs a callback in the middle of the pipeline run, to interleave another delivery."""

    def __init__(self, inner: InvoicePipeline, during_run: Callable[[], None] | None) -> None:
        self.inner = inner
        self.during_run = during_run

    def run(self, pdf: bytes) -> PipelineResult:
        if self.during_run is not None:
            hook, self.during_run = self.during_run, None  # only the first run is interrupted
            hook()
        return self.inner.run(pdf)


class Harness:
    def __init__(
        self,
        sessions: SessionFactory,
        replies: list[str | BaseException],
        *,
        max_attempts: int = 5,
        max_pending: int = 1000,
        during_run: Callable[[], None] | None = None,
    ) -> None:
        self.sessions = sessions
        self.store = MemoryObjectStore()
        self.provider = ScriptedProvider(replies)
        self.parser = FakeParser()
        self.enqueued: list[uuid.UUID] = []
        self.fail_enqueue = False
        pipeline = HookedPipeline(InvoicePipeline(self.parser, self.provider), during_run)
        self.pipeline = pipeline
        self.service = DocumentService(
            sessions,
            self.store,
            {"invoice": pipeline, "purchase_order": pipeline},
            self._enqueue,
            max_attempts=max_attempts,
            max_pending=max_pending,
        )

    def _enqueue(self, session: Session, version: DocumentVersion) -> None:
        if self.fail_enqueue:
            raise RuntimeError("queue unavailable")
        self.enqueued.append(version.id)

    def ingest(self, data: bytes = PDF, tenant_id: uuid.UUID = DEFAULT_TENANT_ID) -> Any:
        return self.service.ingest(
            tenant_id=tenant_id,
            doc_type="invoice",
            filename="invoice.pdf",
            data=data,
            actor="api:upload",
        )

    def count(self, model: type) -> int:
        with self.sessions() as session:
            return int(session.scalar(select(func.count()).select_from(model)) or 0)

    def actions(self) -> list[str]:
        with self.sessions() as session:
            return list(session.scalars(select(AuditEntry.action).order_by(AuditEntry.id)))

    def version(self, version_id: uuid.UUID) -> DocumentVersion:
        with self.sessions() as session:
            return session.get_one(DocumentVersion, version_id)

    def document(self, document_id: uuid.UUID) -> Document:
        with self.sessions() as session:
            return session.get_one(Document, document_id)


@pytest.fixture
def perfect(raw_invoice_from_label: RawFromLabel) -> str:
    return json.dumps(raw_invoice_from_label(LABEL))


# --- ingest ---------------------------------------------------------------------------------


def test_ingest_stores_the_original_and_queues_the_first_version(
    sessions: SessionFactory,
) -> None:
    harness = Harness(sessions, [])

    result = harness.ingest()

    assert result.created
    document, version = result.document, result.version
    assert (document.sha256, document.size_bytes, document.status) == (SHA, len(PDF), "received")
    assert (document.doc_type, document.filename) == ("invoice", "invoice.pdf")
    assert document.storage_key == original_key(DEFAULT_TENANT_ID, SHA)
    assert harness.store.get(document.storage_key) == PDF
    assert (version.version_no, version.status) == (1, "queued")
    assert harness.enqueued == [version.id]
    assert harness.actions() == ["document.received"]


def test_the_same_file_twice_is_one_document_one_job_one_object(
    sessions: SessionFactory,
) -> None:
    harness = Harness(sessions, [])
    first = harness.ingest()

    second = harness.ingest()

    assert not second.created
    assert second.document.id == first.document.id
    assert second.version is None
    assert harness.count(Document) == 1
    assert harness.count(DocumentVersion) == 1
    assert len(harness.enqueued) == 1
    assert len(harness.store.keys()) == 1
    assert harness.actions() == ["document.received"]  # a duplicate changes nothing


def test_different_files_are_different_documents(sessions: SessionFactory) -> None:
    harness = Harness(sessions, [])

    first, second = harness.ingest(PDF), harness.ingest(OTHER_PDF)

    assert first.document.id != second.document.id
    assert len(harness.enqueued) == 2


def test_the_same_file_for_another_tenant_is_a_separate_document(
    sessions: SessionFactory, other_tenant: uuid.UUID
) -> None:
    harness = Harness(sessions, [])

    mine, theirs = harness.ingest(), harness.ingest(tenant_id=other_tenant)

    assert theirs.created
    assert mine.document.id != theirs.document.id
    assert mine.document.storage_key != theirs.document.storage_key


def test_an_unknown_document_type_is_refused(sessions: SessionFactory) -> None:
    harness = Harness(sessions, [])

    with pytest.raises(UnknownDocumentType, match="passport"):
        harness.service.ingest(
            tenant_id=DEFAULT_TENANT_ID,
            doc_type="passport",
            filename="p.pdf",
            data=PDF,
            actor="api:upload",
        )

    assert harness.count(Document) == 0


def test_if_the_job_cannot_be_queued_no_document_is_recorded(sessions: SessionFactory) -> None:
    harness = Harness(sessions, [])
    harness.fail_enqueue = True

    with pytest.raises(RuntimeError, match="queue unavailable"):
        harness.ingest()

    assert harness.count(Document) == 0
    assert harness.count(DocumentVersion) == 0
    assert harness.count(AuditEntry) == 0
    harness.fail_enqueue = False
    assert harness.ingest().created  # and a retry of the upload works


# --- processing -----------------------------------------------------------------------------


def test_processing_records_the_extraction_and_how_it_was_made(
    sessions: SessionFactory, perfect: str
) -> None:
    harness = Harness(sessions, [perfect])
    ingested = harness.ingest()

    outcome = harness.service.process(ingested.version.id)

    assert outcome == "succeeded"
    version = harness.version(ingested.version.id)
    assert (version.status, version.attempts, version.error) == ("succeeded", 1, None)
    assert (version.parser_version, version.model_id) == ("fake 0", "fake-1")
    assert (version.schema_version, version.prompt_version) == ("invoice-1", "invoice-v1")
    assert version.started_at is not None
    assert version.finished_at is not None
    document = harness.document(ingested.document.id)
    assert (document.status, document.page_count) == ("extracted", 1)
    with sessions() as session:
        extraction = session.scalars(select(Extraction)).one()
        run = session.scalars(select(ModelRun)).one()
        parse = session.scalars(select(ParseOutput)).one()
    assert extraction.data["invoice_no"]["value"] == LABEL["invoice"]["invoice_no"]
    assert (
        extraction.sha256
        == hashlib.sha256(audit.canonical_json(extraction.data).encode("utf-8")).hexdigest()
    )
    assert (run.call_no, run.model, run.input_tokens, run.output_tokens) == (1, "fake-1", 100, 50)
    assert ParsedDocument.model_validate(parse.data) == PARSED


def test_processing_writes_an_audit_trail_that_verifies(
    sessions: SessionFactory, perfect: str
) -> None:
    harness = Harness(sessions, [perfect])
    ingested = harness.ingest()

    harness.service.process(ingested.version.id)

    assert harness.actions() == ["document.received", "processing.started", "extraction.created"]
    trail = harness.service.audit_trail(DEFAULT_TENANT_ID, ingested.document.id)
    assert [entry.action for entry in trail] == harness.actions()
    with sessions() as session:
        created = session.scalars(select(AuditEntry).order_by(AuditEntry.id.desc())).first()
        extraction = session.scalars(select(Extraction)).one()
        assert audit.verify_chain(session, DEFAULT_TENANT_ID).ok
    assert created is not None
    assert created.details["extraction_sha256"] == extraction.sha256
    assert created.actor == "system:worker"


def test_a_redelivered_job_does_not_extract_twice(sessions: SessionFactory, perfect: str) -> None:
    harness = Harness(sessions, [perfect])
    ingested = harness.ingest()
    harness.service.process(ingested.version.id)

    outcome = harness.service.process(ingested.version.id)

    assert outcome == "skipped"
    assert harness.count(Extraction) == 1
    assert len(harness.provider.requests) == 1


def test_a_reply_that_never_fits_the_schema_fails_the_version_for_good(
    sessions: SessionFactory,
) -> None:
    harness = Harness(sessions, ["not json", "{}"])
    ingested = harness.ingest()

    outcome = harness.service.process(ingested.version.id)

    assert outcome == "failed"
    version = harness.version(ingested.version.id)
    assert version.status == "failed"
    assert version.error == "The model reply did not fit the schema."
    assert harness.document(ingested.document.id).status == "failed"
    assert harness.count(Extraction) == 0
    assert harness.actions()[-1] == "processing.failed"


def test_an_unreadable_original_fails_the_version_for_good(sessions: SessionFactory) -> None:
    harness = Harness(sessions, [])
    ingested = harness.ingest(b"%PDF-1.4\nnot really a pdf")

    assert harness.service.process(ingested.version.id) == "failed"
    assert harness.version(ingested.version.id).error == "The file could not be read as a PDF."


def test_a_missing_original_fails_the_version_for_good(sessions: SessionFactory) -> None:
    harness = Harness(sessions, [])
    ingested = harness.ingest()
    harness.store.delete(ingested.document.storage_key)

    assert harness.service.process(ingested.version.id) == "failed"
    assert harness.version(ingested.version.id).error == "The stored original is missing."


def test_a_provider_failure_is_handed_back_to_the_queue_to_retry(
    sessions: SessionFactory, perfect: str
) -> None:
    harness = Harness(sessions, [LLMError("status 503 UNAVAILABLE"), perfect])
    ingested = harness.ingest()

    with pytest.raises(TransientProcessingError):
        harness.service.process(ingested.version.id)

    waiting = harness.version(ingested.version.id)
    assert (waiting.status, waiting.attempts) == ("queued", 1)
    assert harness.document(ingested.document.id).status == "processing"
    assert harness.actions()[-1] == "processing.retry_scheduled"

    assert harness.service.process(ingested.version.id) == "succeeded"
    assert harness.version(ingested.version.id).attempts == 2
    assert harness.count(Extraction) == 1


def test_a_provider_failure_on_the_last_attempt_fails_the_version(
    sessions: SessionFactory,
) -> None:
    harness = Harness(sessions, [LLMError("status 503 UNAVAILABLE")], max_attempts=1)
    ingested = harness.ingest()

    outcome = harness.service.process(ingested.version.id)

    assert outcome == "failed"
    version = harness.version(ingested.version.id)
    assert (version.status, version.error) == ("failed", "The model provider failed.")
    assert "503" not in (version.error or "")  # provider text stays in the log, not the record


def test_a_worker_killed_mid_job_loses_nothing(sessions: SessionFactory, perfect: str) -> None:
    harness = Harness(sessions, [Killed(), perfect])
    ingested = harness.ingest()

    with pytest.raises(Killed):
        harness.service.process(ingested.version.id)

    assert harness.version(ingested.version.id).status == "running"
    assert harness.count(Extraction) == 0

    assert harness.service.process(ingested.version.id) == "succeeded"  # the redelivery
    assert harness.version(ingested.version.id).attempts == 2
    assert harness.count(Extraction) == 1
    with sessions() as session:
        assert audit.verify_chain(session, DEFAULT_TENANT_ID).ok


def test_processing_an_unknown_version_is_an_error(sessions: SessionFactory) -> None:
    with pytest.raises(DocumentNotFound):
        Harness(sessions, []).service.process(uuid.uuid4())


# --- versions and reads ---------------------------------------------------------------------


def test_reprocessing_adds_a_version_and_keeps_the_earlier_extraction(
    sessions: SessionFactory, perfect: str, raw_invoice_from_label: RawFromLabel
) -> None:
    changed = raw_invoice_from_label(LABEL)
    changed["invoice_no"]["text"] = "CHANGED/1"
    harness = Harness(sessions, [perfect, json.dumps(changed)])
    ingested = harness.ingest()
    harness.service.process(ingested.version.id)

    second = harness.service.reprocess(
        tenant_id=DEFAULT_TENANT_ID, document_id=ingested.document.id, actor="api:reprocess"
    )
    harness.service.process(second.id)

    assert second.version_no == 2
    assert harness.enqueued == [ingested.version.id, second.id]
    with sessions() as session:
        stored = list(session.scalars(select(Extraction).order_by(Extraction.created_at)))
    assert [e.data["invoice_no"]["value"] for e in stored] == [
        LABEL["invoice"]["invoice_no"],
        "CHANGED/1",
    ]
    latest = harness.service.latest_extraction(DEFAULT_TENANT_ID, ingested.document.id)
    assert latest is not None
    assert latest.version.version_no == 2
    assert latest.extraction.data["invoice_no"]["value"] == "CHANGED/1"
    assert [run.call_no for run in latest.model_runs] == [1]
    assert "document.reprocess_requested" in harness.actions()


def test_detail_lists_the_versions_in_order(sessions: SessionFactory, perfect: str) -> None:
    harness = Harness(sessions, [perfect])
    ingested = harness.ingest()
    harness.service.process(ingested.version.id)
    harness.service.reprocess(
        tenant_id=DEFAULT_TENANT_ID, document_id=ingested.document.id, actor="api:reprocess"
    )

    detail = harness.service.detail(DEFAULT_TENANT_ID, ingested.document.id)

    assert detail.document.id == ingested.document.id
    assert [version.version_no for version in detail.versions] == [1, 2]


def test_there_is_no_extraction_before_processing(sessions: SessionFactory) -> None:
    harness = Harness(sessions, [])
    ingested = harness.ingest()

    assert harness.service.latest_extraction(DEFAULT_TENANT_ID, ingested.document.id) is None


def test_another_tenant_cannot_read_or_reprocess_a_document(
    sessions: SessionFactory, other_tenant: uuid.UUID
) -> None:
    harness = Harness(sessions, [])
    ingested = harness.ingest()

    with pytest.raises(DocumentNotFound):
        harness.service.detail(other_tenant, ingested.document.id)
    with pytest.raises(DocumentNotFound):
        harness.service.latest_extraction(other_tenant, ingested.document.id)
    with pytest.raises(DocumentNotFound):
        harness.service.audit_trail(other_tenant, ingested.document.id)
    with pytest.raises(DocumentNotFound):
        harness.service.reprocess(
            tenant_id=other_tenant, document_id=ingested.document.id, actor="api:reprocess"
        )


# --- review follow-ups: races, bounds and integrity -----------------------------------------


def test_the_audit_log_does_not_record_the_filename(sessions: SessionFactory) -> None:
    """The log can never be edited, so it must not hold what may need erasing later."""
    harness = Harness(sessions, [])
    harness.ingest()

    with sessions() as session:
        details = session.scalars(select(AuditEntry.details)).one()
    assert set(details) == {"sha256", "size_bytes", "doc_type"}


def test_a_duplicate_upload_restores_a_lost_original(sessions: SessionFactory) -> None:
    harness = Harness(sessions, [])
    first = harness.ingest()
    harness.store.delete(first.document.storage_key)

    harness.ingest()

    assert harness.store.get(first.document.storage_key) == PDF


def test_the_same_file_as_a_different_document_type_is_a_conflict(
    sessions: SessionFactory,
) -> None:
    harness = Harness(sessions, [])
    harness.ingest()

    with pytest.raises(DocumentTypeConflict, match="invoice"):
        harness.service.ingest(
            tenant_id=DEFAULT_TENANT_ID,
            doc_type="purchase_order",
            filename="po.pdf",
            data=PDF,
            actor="api:upload",
        )


def test_uploads_are_refused_while_too_many_documents_are_waiting(
    sessions: SessionFactory,
) -> None:
    harness = Harness(sessions, [], max_pending=1)
    harness.ingest(PDF)

    with pytest.raises(QueueFull):
        harness.ingest(OTHER_PDF)

    assert harness.count(Document) == 1
    assert not harness.ingest(PDF).created  # a duplicate is still answered


def test_a_changed_original_is_not_extracted(sessions: SessionFactory, perfect: str) -> None:
    harness = Harness(sessions, [perfect])
    ingested = harness.ingest()
    harness.store.put(ingested.document.storage_key, OTHER_PDF, "application/pdf")

    assert harness.service.process(ingested.version.id) == "failed"
    assert harness.version(ingested.version.id).error == (
        "The stored original does not match its recorded hash."
    )
    assert harness.provider.requests == []


def test_a_bug_inside_a_pipeline_is_not_reported_as_an_unknown_document_type(
    sessions: SessionFactory,
) -> None:
    harness = Harness(sessions, [KeyError("some internal key")], max_attempts=1)
    ingested = harness.ingest()

    assert harness.service.process(ingested.version.id) == "failed"
    assert harness.version(ingested.version.id).error == "Internal error."


def test_reprocessing_is_refused_while_a_version_is_still_in_flight(
    sessions: SessionFactory,
) -> None:
    harness = Harness(sessions, [])
    ingested = harness.ingest()

    with pytest.raises(ReprocessInProgress):
        harness.service.reprocess(
            tenant_id=DEFAULT_TENANT_ID, document_id=ingested.document.id, actor="api:reprocess"
        )

    assert harness.count(DocumentVersion) == 1


def test_a_late_failure_cannot_undo_a_version_another_delivery_finished(
    sessions: SessionFactory, perfect: str
) -> None:
    """Delivery A stalls, the job is redelivered as B and succeeds, then A fails."""
    harness = Harness(sessions, [perfect, LLMError("status 503")], max_attempts=5)
    ingested = harness.ingest()
    version_id = ingested.version.id
    harness.pipeline.during_run = lambda: harness.service.process(version_id)  # delivery B

    outcome = harness.service.process(version_id)  # delivery A

    assert outcome == "skipped"
    version = harness.version(version_id)
    assert (version.status, version.error) == ("succeeded", None)
    assert harness.document(ingested.document.id).status == "extracted"
    assert harness.count(Extraction) == 1
    assert harness.actions()[-1] == "extraction.created"


def test_a_late_success_does_not_write_a_second_extraction(
    sessions: SessionFactory, perfect: str
) -> None:
    harness = Harness(sessions, [perfect, perfect])
    ingested = harness.ingest()
    version_id = ingested.version.id
    harness.pipeline.during_run = lambda: harness.service.process(version_id)

    assert harness.service.process(version_id) == "skipped"
    assert harness.count(Extraction) == 1
    assert harness.count(ModelRun) == 1
    assert harness.actions().count("extraction.created") == 1


def test_a_document_that_keeps_killing_its_worker_is_failed_not_retried_forever(
    sessions: SessionFactory, perfect: str
) -> None:
    harness = Harness(sessions, [Killed(), Killed(), perfect], max_attempts=2)
    ingested = harness.ingest()
    for _ in range(2):
        with pytest.raises(Killed):
            harness.service.process(ingested.version.id)

    outcome = harness.service.process(ingested.version.id)  # the third delivery

    assert outcome == "failed"
    version = harness.version(ingested.version.id)
    assert (version.status, version.attempts) == ("failed", 2)
    assert version.error == "Processing was interrupted too many times."
    assert len(harness.provider.requests) == 2  # the third delivery did not run the pipeline


def test_many_documents_processed_at_once_all_finish_and_the_chain_holds(
    sessions: SessionFactory, raw_invoice_from_label: RawFromLabel
) -> None:
    pairs = [f"pair_{n:03d}" for n in range(1, 9)]
    replies: list[str | BaseException] = []
    harness = Harness(sessions, replies)
    version_ids = []
    for pair in pairs:
        label = json.loads((FIXTURES / pair / "label.json").read_text(encoding="utf-8"))
        replies.append(json.dumps(raw_invoice_from_label(label)))
        ingested = harness.ingest((FIXTURES / pair / "invoice.pdf").read_bytes())
        version_ids.append(ingested.version.id)
    harness.provider.replies = [replies[0]] * len(pairs)  # any valid reply will do

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(harness.service.process, version_ids))

    assert outcomes == ["succeeded"] * len(pairs)
    assert harness.count(Extraction) == len(pairs)
    with sessions() as session:
        report = audit.verify_chain(session, DEFAULT_TENANT_ID)
    assert (report.consistent, report.entries) == (True, 3 * len(pairs))
