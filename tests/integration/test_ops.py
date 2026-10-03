"""Operational alerts: `python -m docforge.ops check` reads the queue and processing state and
says what needs a person, with an exit code a scheduler can alarm on. Counts only: no tenant
ids or document content leave in the report."""

import json
import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, Engine

from docforge.db import DEFAULT_TENANT_ID
from docforge.ops import Thresholds, check, main, snapshot

pytestmark = pytest.mark.integration

Run = Callable[..., None]


@pytest.fixture
def run(owner_engine: Engine) -> Run:
    def execute(sql: str, **params: object) -> None:
        with owner_engine.begin() as conn:
            conn.execute(text(sql), params)

    return execute


def job(run: Run, queue: str, status: str, *, age_minutes: float = 0) -> None:
    run(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, status) "
        "VALUES (:queue, 't', 'todo')",
        queue=queue,
    )
    # As a worker does: a job is taken (doing) before it ends; the queue logs events by
    # transition, and only doing -> failed is a failure.
    for step in ("doing", status) if status != "todo" else ():
        run(
            "UPDATE procrastinate_jobs SET status = CAST(:status AS procrastinate_job_status) "
            "WHERE id = (SELECT max(id) FROM procrastinate_jobs)",
            status=step,
        )
    run(
        "UPDATE procrastinate_events SET at = now() - make_interval(mins => :age) "
        "WHERE job_id = (SELECT max(id) FROM procrastinate_jobs)",
        age=age_minutes,
    )


def worker(run: Run, *, silent_seconds: int = 0) -> None:
    run(
        "INSERT INTO procrastinate_workers (last_heartbeat) "
        "VALUES (now() - make_interval(secs => :silent))",
        silent=silent_seconds,
    )


def versions(run: Run, statuses: list[str]) -> None:
    for status in statuses:
        run(
            "WITH d AS (INSERT INTO documents (tenant_id, doc_type, sha256, storage_key, filename, "
            "size_bytes) VALUES (:tenant, 'invoice', :sha, 'k', 'a.pdf', 1) RETURNING id) "
            "INSERT INTO document_versions (tenant_id, document_id, version_no, status, "
            "finished_at) SELECT :tenant, id, 1, :status, now() FROM d",
            tenant=DEFAULT_TENANT_ID,
            sha=uuid.uuid4().hex * 2,
            status=status,
        )


def names(owner_engine: Engine) -> set[str]:
    return {alert.name for alert in check(snapshot(owner_engine), Thresholds())}


def test_a_quiet_system_raises_nothing(owner_engine: Engine, run: Run) -> None:
    worker(run)
    versions(run, ["succeeded"] * 5)

    assert names(owner_engine) == set()


def test_waiting_work_with_no_live_worker_is_critical(owner_engine: Engine, run: Run) -> None:
    worker(run, silent_seconds=600)
    job(run, "extract", "todo")

    alerts = check(snapshot(owner_engine), Thresholds())

    assert [(a.name, a.severity) for a in alerts] == [("no_worker", "critical")]


def test_a_job_waiting_too_long_is_critical_and_a_backlog_is_a_warning(
    owner_engine: Engine, run: Run
) -> None:
    worker(run)
    job(run, "extract", "todo", age_minutes=30)
    for _ in range(3):
        job(run, "index", "todo")

    alerts = {a.name: a for a in check(snapshot(owner_engine), Thresholds(backlog=2))}

    assert alerts["queue_stalled"].severity == "critical"
    assert "extract" in alerts["queue_stalled"].message
    assert alerts["backlog"].severity == "warning"
    assert "index" in alerts["backlog"].message


def test_jobs_failed_in_the_last_hour_are_reported_by_queue(owner_engine: Engine, run: Run) -> None:
    worker(run)
    job(run, "webhooks", "failed", age_minutes=10)
    job(run, "extract", "failed", age_minutes=180)  # long ago: already seen

    alerts = {a.name: a for a in check(snapshot(owner_engine), Thresholds())}

    assert set(alerts) == {"jobs_failed"}
    assert "webhooks" in alerts["jobs_failed"].message
    assert "extract" not in alerts["jobs_failed"].message


def test_a_high_document_failure_rate_is_a_warning_once_there_are_enough_documents(
    owner_engine: Engine, run: Run
) -> None:
    worker(run)
    versions(run, ["failed", "succeeded"])
    assert "documents_failing" not in names(owner_engine)  # two documents say little

    versions(run, ["failed", "failed", "succeeded"])
    assert "documents_failing" in names(owner_engine)


def test_the_report_holds_counts_only(owner_engine: Engine, run: Run) -> None:
    worker(run)
    versions(run, ["failed"] * 5)

    report = json.dumps(snapshot(owner_engine).as_dict())

    assert str(DEFAULT_TENANT_ID) not in report
    assert "a.pdf" not in report


def test_the_command_exits_by_the_worst_alert(
    empty_database_url: URL, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    url = empty_database_url.render_as_string(hide_password=False)
    worker(run)
    assert main(["check", "--database-url", url]) == 0

    job(run, "webhooks", "failed", age_minutes=1)
    assert main(["check", "--database-url", url]) == 1

    run("UPDATE procrastinate_workers SET last_heartbeat = now() - interval '1 hour'")
    job(run, "extract", "todo")
    assert main(["check", "--database-url", url]) == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "critical"


def test_an_unreachable_database_is_critical_not_a_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "postgresql+psycopg://nobody:wrong@127.0.0.1:1/none"

    assert main(["check", "--database-url", url]) == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "critical"
    assert out["alerts"][0]["name"] == "ops_unreachable"
    assert "wrong" not in json.dumps(out)  # no credentials in the report
