"""The Procrastinate queue on a real database: transactional enqueue, retries, recovery."""

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import DocumentVersion
from docforge.db.session import SessionFactory
from docforge.documents import DocumentService, IngestResult
from docforge.extraction.pipeline import InvoicePipeline
from docforge.llm.base import LLMError
from docforge.queue import QUEUE_NAME, JobQueue
from docforge.storage import MemoryObjectStore
from docforge.worker import requeue_stalled, run_worker
from fakes import FakeParser, ScriptedProvider

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PDF = (FIXTURES / "pair_001" / "invoice.pdf").read_bytes()
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))
MAX_ATTEMPTS = 3

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


class Setup:
    def __init__(
        self, engine: Engine, sessions: SessionFactory, replies: Sequence[str | BaseException]
    ) -> None:
        self.engine = engine
        self.sessions = sessions
        self.provider = ScriptedProvider(replies)
        self.queue = JobQueue(engine.url, max_attempts=MAX_ATTEMPTS, retry_wait_seconds=0)
        self.fail_after_enqueue = False
        self.service = DocumentService(
            sessions,
            MemoryObjectStore(),
            {"invoice": InvoicePipeline(FakeParser(), self.provider)},
            self._enqueue,
            max_attempts=MAX_ATTEMPTS,
        )
        self.queue.bind(self.service)

    def _enqueue(self, session: Session, version: DocumentVersion) -> None:
        self.queue.enqueue(session, version)
        if self.fail_after_enqueue:
            raise RuntimeError("something else in the transaction failed")

    def ingest(self) -> IngestResult:
        return self.service.ingest(
            tenant_id=DEFAULT_TENANT_ID,
            doc_type="invoice",
            filename="invoice.pdf",
            data=PDF,
            actor="api:upload",
        )

    def jobs(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT status::text, args, queue_name FROM procrastinate_jobs ORDER BY id")
            )
            return [dict(row._mapping) for row in rows]

    def work(self) -> None:
        """Run a worker until the queue has nothing ready."""
        asyncio.run(run_worker(self.queue, wait=False))

    def version(self, ingested: IngestResult) -> DocumentVersion:
        assert ingested.version is not None
        with self.sessions() as session:
            return session.get_one(DocumentVersion, ingested.version.id)

    def extractions(self) -> int:
        with self.sessions() as session:
            return len(list(session.scalars(text("SELECT id FROM extractions"))))


@pytest.fixture
def perfect(raw_invoice_from_label: RawFromLabel) -> str:
    return json.dumps(raw_invoice_from_label(LABEL))


def test_ingest_creates_one_job_for_the_new_version(
    engine: Engine, sessions: SessionFactory
) -> None:
    setup = Setup(engine, sessions, [])

    ingested = setup.ingest()

    assert ingested.version is not None
    assert setup.jobs() == [
        {
            "status": "todo",
            "args": {"version_id": str(ingested.version.id)},
            "queue_name": QUEUE_NAME,
        }
    ]


def test_a_duplicate_upload_creates_no_second_job(engine: Engine, sessions: SessionFactory) -> None:
    setup = Setup(engine, sessions, [])
    setup.ingest()

    setup.ingest()

    assert len(setup.jobs()) == 1


def test_the_job_is_rolled_back_with_the_rest_of_the_ingest(
    engine: Engine, sessions: SessionFactory
) -> None:
    setup = Setup(engine, sessions, [])
    setup.fail_after_enqueue = True

    with pytest.raises(RuntimeError):
        setup.ingest()

    assert setup.jobs() == []


def test_a_worker_processes_the_job(engine: Engine, sessions: SessionFactory, perfect: str) -> None:
    setup = Setup(engine, sessions, [perfect])
    ingested = setup.ingest()

    setup.work()

    assert setup.version(ingested).status == "succeeded"
    assert [job["status"] for job in setup.jobs()] == ["succeeded"]
    assert setup.extractions() == 1


def test_a_provider_failure_is_retried_by_the_queue(
    engine: Engine, sessions: SessionFactory, perfect: str
) -> None:
    setup = Setup(engine, sessions, [LLMError("status 503"), perfect])
    ingested = setup.ingest()

    for _ in range(MAX_ATTEMPTS):
        setup.work()

    version = setup.version(ingested)
    assert (version.status, version.attempts) == ("succeeded", 2)
    assert setup.extractions() == 1


def test_after_the_last_attempt_the_version_is_failed_not_retried_forever(
    engine: Engine, sessions: SessionFactory
) -> None:
    setup = Setup(engine, sessions, [LLMError("status 503")] * 10)
    ingested = setup.ingest()

    for _ in range(MAX_ATTEMPTS + 2):
        setup.work()

    version = setup.version(ingested)
    assert (version.status, version.attempts) == ("failed", MAX_ATTEMPTS)
    assert version.error == "The model provider failed."
    assert len(setup.provider.requests) == MAX_ATTEMPTS


def test_a_job_whose_worker_died_is_put_back_on_the_queue(
    engine: Engine, sessions: SessionFactory, perfect: str
) -> None:
    setup = Setup(engine, sessions, [perfect])
    ingested = setup.ingest()
    with engine.begin() as conn:  # a worker took the job, then stopped sending heartbeats
        worker_id = conn.execute(
            text(
                "INSERT INTO procrastinate_workers (last_heartbeat) "
                "VALUES (now() - interval '1 hour') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text("UPDATE procrastinate_jobs SET status = 'doing', worker_id = :worker"),
            {"worker": worker_id},
        )

    async def recover() -> int:
        async with setup.queue.app.open_async():
            return await requeue_stalled(setup.queue.app, stalled_after_seconds=30)

    assert asyncio.run(recover()) == 1
    assert [job["status"] for job in setup.jobs()] == ["todo"]

    setup.work()
    assert setup.version(ingested).status == "succeeded"


def test_a_job_with_a_live_worker_is_left_alone(engine: Engine, sessions: SessionFactory) -> None:
    setup = Setup(engine, sessions, [])
    setup.ingest()
    with engine.begin() as conn:
        worker_id = conn.execute(
            text("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now()) RETURNING id")
        ).scalar_one()
        conn.execute(
            text("UPDATE procrastinate_jobs SET status = 'doing', worker_id = :worker"),
            {"worker": worker_id},
        )

    async def recover() -> int:
        async with setup.queue.app.open_async():
            return await requeue_stalled(setup.queue.app, stalled_after_seconds=30)

    assert asyncio.run(recover()) == 0
    assert [job["status"] for job in setup.jobs()] == ["doing"]


def test_a_database_error_in_the_task_is_retried_not_dropped(
    engine: Engine, sessions: SessionFactory, perfect: str
) -> None:
    """Anything unexpected must leave the job on the queue, or the version would be stuck."""
    setup = Setup(engine, sessions, [perfect])
    ingested = setup.ingest()
    real_process, calls = setup.service.process, []

    def flaky(version_id: Any) -> Any:
        calls.append(version_id)
        if len(calls) == 1:
            raise OperationalError("SELECT 1", {}, Exception("connection reset"))
        return real_process(version_id)

    setup.service.process = flaky  # type: ignore[method-assign]

    for _ in range(MAX_ATTEMPTS):
        setup.work()

    assert len(calls) == 2
    assert setup.version(ingested).status == "succeeded"


def test_a_job_for_a_version_that_no_longer_exists_is_dropped(
    engine: Engine, sessions: SessionFactory
) -> None:
    setup = Setup(engine, sessions, [])
    setup.ingest()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE procrastinate_jobs "
                "SET args = jsonb_build_object('version_id', CAST(:id AS text))"
            ),
            {"id": "00000000-0000-0000-0000-00000000dead"},
        )

    setup.work()

    assert [job["status"] for job in setup.jobs()] == ["succeeded"]  # nothing left to retry
