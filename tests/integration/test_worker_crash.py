"""The C2 gate with real processes: kill a worker mid-job and nothing is lost.

Starts `python -m docforge.worker` as a subprocess against the Compose Postgres and MinIO,
kills it with SIGKILL while it holds a job, starts a second worker, and checks the document
is extracted exactly once.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine

from docforge import audit
from docforge.config import Settings
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry, DocumentVersion, Extraction
from docforge.db.session import SessionFactory
from docforge.documents import DocumentService
from docforge.queue import JobQueue
from docforge.storage import S3ObjectStore
from worker_fixtures import BLOCK_FILE_VAR, REPLY_FILE_VAR, BlockingPipeline

pytestmark = pytest.mark.integration

TESTS = Path(__file__).resolve().parents[1]
REPO = TESTS.parent
PDF = (TESTS / "fixtures" / "synthetic" / "pair_005" / "invoice.pdf").read_bytes()
LABEL = json.loads(
    (TESTS / "fixtures" / "synthetic" / "pair_005" / "label.json").read_text(encoding="utf-8")
)

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def wait_for(condition: Callable[[], bool], what: str, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}")


@pytest.fixture
def worker_env(
    engine: Engine, tmp_path: Path, raw_invoice_from_label: RawFromLabel
) -> dict[str, str]:
    block, reply = tmp_path / "block", tmp_path / "reply.json"
    block.touch()
    reply.write_text(json.dumps(raw_invoice_from_label(LABEL)), encoding="utf-8")
    return {
        **os.environ,
        "DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "PIPELINE_FACTORY": "worker_fixtures:build_pipelines",
        "PYTHONPATH": str(TESTS),
        BLOCK_FILE_VAR: str(block),
        REPLY_FILE_VAR: str(reply),
        "WORKER_HEARTBEAT_SECONDS": "0.5",
        "WORKER_STALLED_AFTER_SECONDS": "2",
        "JOB_RETRY_WAIT_SECONDS": "0",
        "GEMINI_API_KEY": "",  # no model or embedding calls from a test
    }


@pytest.fixture
def start_worker(worker_env: dict[str, str]) -> Iterator[Callable[[], subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []

    def start() -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [sys.executable, "-m", "docforge.worker"], cwd=REPO, env=worker_env
        )
        started.append(process)
        return process

    yield start
    for process in started:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def test_killing_a_worker_mid_job_loses_nothing(
    engine: Engine,
    sessions: SessionFactory,
    settings: Settings,
    worker_env: dict[str, str],
    start_worker: Callable[[], subprocess.Popen[bytes]],
) -> None:
    queue = JobQueue(engine.url)
    # This process only ingests; the pipeline here is never run.
    store = S3ObjectStore.from_settings(settings)
    service = DocumentService(sessions, store, {"invoice": BlockingPipeline()}, queue.enqueue)
    ingested = service.ingest(
        tenant_id=DEFAULT_TENANT_ID,
        doc_type="invoice",
        filename="invoice.pdf",
        data=PDF,
        actor="api:upload",
    )
    assert ingested.version is not None
    version_id = ingested.version.id

    def version() -> DocumentVersion:
        with sessions() as session:
            return session.get_one(DocumentVersion, version_id)

    first = start_worker()
    wait_for(lambda: version().status == "running", "the first worker to take the job")

    first.send_signal(signal.SIGKILL)  # no chance to clean up
    first.wait(timeout=10)
    assert version().status == "running"  # the job is orphaned, the work is not done

    Path(worker_env[BLOCK_FILE_VAR]).unlink()  # the next attempt may finish
    second = start_worker()
    wait_for(lambda: version().status == "succeeded", "the second worker to finish the job")

    assert version().attempts == 2
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Extraction)) == 1
        actions = list(session.scalars(select(AuditEntry.action).order_by(AuditEntry.id)))
        assert audit.verify_chain(session, DEFAULT_TENANT_ID).consistent
    assert actions == [
        "document.received",
        "processing.started",
        "processing.started",
        "processing.stage",  # extracting
        "processing.stage",  # checking
        "extraction.created",
        "assessment.created",
        "processing.stage",  # processed (or indexing, where search is wired in)
    ]

    def extract_jobs() -> list[str]:
        with engine.connect() as conn:
            query = "SELECT status::text FROM procrastinate_jobs WHERE queue_name = 'extract'"
            return list(conn.execute(text(query)).scalars().all())

    # The queue marks its job done just after the task returns, so a moment after the
    # version is: wait for it rather than catch it in between (seen on a busy CI runner).
    wait_for(lambda: extract_jobs() == ["succeeded"], "the queue to record the job as done")
    assert extract_jobs() == ["succeeded"]  # (a search-index job is queued as well)

    second.send_signal(signal.SIGTERM)  # and a worker asked to stop does so cleanly
    assert second.wait(timeout=20) == 0
