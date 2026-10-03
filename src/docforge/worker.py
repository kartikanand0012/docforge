"""The worker process: `python -m docforge.worker`.

Runs queued jobs one at a time and puts back any job whose worker stopped sending
heartbeats, so a crashed or killed worker does not strand a document.
"""

import asyncio
import contextlib
import logging

import procrastinate

from docforge.config import get_settings
from docforge.queue import QUEUES, JobQueue

logger = logging.getLogger(__name__)


async def requeue_stalled(app: procrastinate.App, *, stalled_after_seconds: float) -> int:
    """Put jobs held by dead workers back on the queue. Returns how many."""
    jobs = []
    for queue in QUEUES:
        jobs += list(
            await app.job_manager.get_stalled_jobs(
                queue=queue, seconds_since_heartbeat=stalled_after_seconds
            )
        )
    requeued = 0
    for job in jobs:
        try:
            await app.job_manager.retry_job(job)
        except Exception:
            # Another worker got there first, or the job has moved on. Carry on with the rest.
            logger.warning("could not requeue job %s; skipping it", job.id, exc_info=True)
            continue
        logger.warning("requeued job %s: its worker stopped responding", job.id)
        requeued += 1
    return requeued


async def _requeue_stalled_forever(app: procrastinate.App, stalled_after_seconds: float) -> None:
    while True:
        await asyncio.sleep(stalled_after_seconds / 2)
        try:
            await requeue_stalled(app, stalled_after_seconds=stalled_after_seconds)
        except Exception:
            logger.exception("could not check for stalled jobs; will try again")


async def run_worker(
    queue: JobQueue,
    *,
    wait: bool = True,
    heartbeat_seconds: float = 10,
    stalled_after_seconds: float = 30,
) -> None:
    """Run jobs until stopped by a signal. With `wait=False`, stop when nothing is ready."""
    async with queue.app.open_async():
        try:
            await requeue_stalled(queue.app, stalled_after_seconds=stalled_after_seconds)
        except Exception:
            logger.exception("could not check for stalled jobs at startup; will try again")
        watcher = asyncio.create_task(_requeue_stalled_forever(queue.app, stalled_after_seconds))
        try:
            await queue.app.run_worker_async(
                queues=QUEUES,
                # The parser takes one document at a time anyway; the second slot lets a
                # webhook be delivered while a long document is being read.
                concurrency=2,
                wait=wait,
                update_heartbeat_interval=heartbeat_seconds,
                stalled_worker_timeout=stalled_after_seconds,
                install_signal_handlers=wait,
            )
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher


def main() -> None:
    from docforge.wiring import build_service

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    _service, queue = build_service(settings)
    asyncio.run(
        run_worker(
            queue,
            heartbeat_seconds=settings.worker_heartbeat_seconds,
            stalled_after_seconds=settings.worker_stalled_after_seconds,
        )
    )


if __name__ == "__main__":
    main()
