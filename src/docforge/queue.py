"""The job queue: Procrastinate, in the same Postgres database as the documents.

A job is enqueued on the caller's connection, inside the transaction that creates the
version it refers to, so there is never a version without a job or a job without a version.
"""

import logging
import math
import uuid
from typing import TYPE_CHECKING

import procrastinate
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from docforge.db.models import DocumentVersion
from docforge.documents import DocumentNotFound

if TYPE_CHECKING:
    from docforge.documents import DocumentService

logger = logging.getLogger(__name__)

QUEUE_NAME = "extract"
TASK_NAME = "process_document_version"
DEFAULT_MAX_ATTEMPTS = 5


def _libpq_url(url: URL | str) -> str:
    """The SQLAlchemy URL without its driver suffix, as libpq expects."""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


class JobQueue:
    def __init__(
        self,
        database_url: URL | str,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_wait_seconds: float = 5,
    ) -> None:
        self.max_attempts = max_attempts
        self.app = procrastinate.App(
            connector=procrastinate.PsycopgConnector(conninfo=_libpq_url(database_url))
        )
        self._service: DocumentService | None = None

        # Any exception puts the job back, with a wait that grows by `retry_wait_seconds`
        # per attempt: an unexpected error must not strand a version. The service keeps the
        # real budget (the version's attempts) and fails the version when it runs out, so the
        # queue only needs to keep delivering a little longer than that.
        wait = math.ceil(retry_wait_seconds)

        @self.app.task(
            name=TASK_NAME,
            queue=QUEUE_NAME,
            retry=procrastinate.RetryStrategy(
                max_attempts=max_attempts + 2, wait=wait, linear_wait=wait
            ),
        )
        def process_document_version(version_id: str) -> None:
            if self._service is None:
                raise RuntimeError("the queue is not bound to a document service")
            try:
                self._service.process(uuid.UUID(version_id))
            except DocumentNotFound:
                logger.error("dropping job for version %s: it does not exist", version_id)

        self._task = process_document_version

    def bind(self, service: "DocumentService") -> None:
        """Give the task the service it runs. The service is built with `enqueue`, so the
        two are connected after both exist."""
        self._service = service

    def enqueue(self, session: Session, version: DocumentVersion) -> None:
        """Add the job for `version` in the session's transaction."""
        connection = session.connection().connection.driver_connection
        self._task.configure(connection=connection).defer(version_id=str(version.id))
