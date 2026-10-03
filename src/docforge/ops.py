"""`python -m docforge.ops check`: what in the running system needs a person.

Reads the queue and processing state across all organisations, so it connects as the owner
(`MIGRATION_DATABASE_URL`), and reports counts only: no tenant ids, file names or content.
Prints one JSON line and exits 0 (nothing), 1 (a warning) or 2 (something critical), for a
scheduler or a CloudWatch alarm to act on.
"""

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Literal

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from docforge.config import get_settings

Severity = Literal["warning", "critical"]


@dataclass(frozen=True)
class Thresholds:
    backlog: int = 200  # jobs waiting in one queue
    oldest_waiting_minutes: float = 15
    worker_silent_seconds: float = 60
    failure_rate: float = 0.2  # of documents finished in the last hour
    min_documents: int = 5  # fewer than this in an hour says little about a rate


@dataclass(frozen=True)
class Queue:
    waiting: int = 0
    running: int = 0
    failed_last_hour: int = 0
    oldest_waiting_minutes: float = 0.0


@dataclass(frozen=True)
class Snapshot:
    queues: dict[str, Queue] = field(default_factory=dict)
    live_workers: int = 0
    documents_finished_last_hour: int = 0
    documents_failed_last_hour: int = 0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Alert:
    name: str
    severity: Severity
    message: str


_QUEUES = text(
    """
    SELECT j.queue_name,
           count(*) FILTER (WHERE j.status = 'todo') AS waiting,
           count(*) FILTER (WHERE j.status = 'doing') AS running,
           count(*) FILTER (WHERE j.status = 'failed' AND e.at > now() - interval '1 hour')
               AS failed_last_hour,
           coalesce(extract(epoch FROM now() - min(e.at) FILTER (WHERE j.status = 'todo')) / 60,
                    0) AS oldest_waiting_minutes
    FROM procrastinate_jobs j
    LEFT JOIN LATERAL (
        SELECT max(at) AS at FROM procrastinate_events WHERE job_id = j.id
    ) e ON true
    WHERE j.status IN ('todo', 'doing', 'failed')
    GROUP BY j.queue_name
    """
)


def snapshot(engine: Engine, thresholds: Thresholds | None = None) -> Snapshot:
    silent = (thresholds or Thresholds()).worker_silent_seconds
    with engine.connect() as conn:
        queues = {
            row.queue_name: Queue(
                waiting=row.waiting,
                running=row.running,
                failed_last_hour=row.failed_last_hour,
                oldest_waiting_minutes=round(float(row.oldest_waiting_minutes), 1),
            )
            for row in conn.execute(_QUEUES)
        }
        live = conn.execute(
            text(
                "SELECT count(*) FROM procrastinate_workers "
                "WHERE last_heartbeat > now() - make_interval(secs => :silent)"
            ),
            {"silent": silent},
        ).scalar_one()
        finished, failed = conn.execute(
            text(
                "SELECT count(*), count(*) FILTER (WHERE status = 'failed') "
                "FROM document_versions "
                "WHERE finished_at > now() - interval '1 hour' "
                "AND status IN ('succeeded', 'failed')"
            )
        ).one()
    return Snapshot(queues, live, finished, failed)


def check(state: Snapshot, thresholds: Thresholds) -> list[Alert]:
    alerts: list[Alert] = []
    waiting = sum(queue.waiting for queue in state.queues.values())
    if waiting and state.live_workers == 0:
        alerts.append(
            Alert("no_worker", "critical", f"{waiting} jobs waiting and no worker is alive")
        )
        return alerts  # everything else follows from this
    stalled = {
        name: queue.oldest_waiting_minutes
        for name, queue in state.queues.items()
        if queue.waiting and queue.oldest_waiting_minutes > thresholds.oldest_waiting_minutes
    }
    if stalled:
        detail = ", ".join(
            f"{name} ({minutes:.0f} min)" for name, minutes in sorted(stalled.items())
        )
        alerts.append(Alert("queue_stalled", "critical", f"jobs waiting too long: {detail}"))
    backlog = {n: q.waiting for n, q in state.queues.items() if q.waiting > thresholds.backlog}
    if backlog:
        detail = ", ".join(f"{name} ({count})" for name, count in sorted(backlog.items()))
        alerts.append(Alert("backlog", "warning", f"queues backing up: {detail}"))
    failed = {n: q.failed_last_hour for n, q in state.queues.items() if q.failed_last_hour}
    if failed:
        detail = ", ".join(f"{name} ({count})" for name, count in sorted(failed.items()))
        alerts.append(Alert("jobs_failed", "warning", f"jobs failed in the last hour: {detail}"))
    finished = state.documents_finished_last_hour
    if (
        finished >= thresholds.min_documents
        and state.documents_failed_last_hour / finished > thresholds.failure_rate
    ):
        alerts.append(
            Alert(
                "documents_failing",
                "warning",
                f"{state.documents_failed_last_hour} of {finished} documents failed "
                "in the last hour",
            )
        )
    return alerts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m docforge.ops", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("check", help="report what needs a person; exit 0, 1 or 2")
    run.add_argument("--database-url", default=None, help="defaults to MIGRATION_DATABASE_URL")
    args = parser.parse_args(argv)

    url = args.database_url or get_settings().migration_database_url.get_secret_value()
    engine = create_engine(url)
    try:
        thresholds = Thresholds()
        state = snapshot(engine, thresholds)
    finally:
        engine.dispose()
    alerts = check(state, thresholds)
    worst = 2 if any(a.severity == "critical" for a in alerts) else 1 if alerts else 0
    status = ("ok", "warning", "critical")[worst]
    print(
        json.dumps(
            {"status": status, "alerts": [asdict(a) for a in alerts], "snapshot": state.as_dict()}
        )
    )
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
