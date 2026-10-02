"""The job queue: Procrastinate, in the same Postgres database as the documents.

A job is enqueued on the caller's connection, inside the transaction that creates the
version it refers to, so there is never a version without a job or a job without a version.
"""

import uuid
from typing import TYPE_CHECKING

import procrastinate
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from docforge.db.models import DocumentVersion
from docforge.documents import TransientProcessingError

if TYPE_CHECKING:
    from docforge.documents import DocumentService

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

        # Only a transient failure is retried, with a wait that grows by `retry_wait_seconds`
        # per attempt. On the last attempt the service fails the version instead of raising.
        @self.app.task(
            name=TASK_NAME,
            queue=QUEUE_NAME,
            pass_context=True,
            retry=procrastinate.RetryStrategy(
                max_attempts=max_attempts,
                wait=int(retry_wait_seconds),
                linear_wait=int(retry_wait_seconds),
                retry_exceptions=[TransientProcessingError],
            ),
        )
        def process_document_version(context: procrastinate.JobContext, version_id: str) -> None:
            if self._service is None:
                raise RuntimeError("the queue is not bound to a document service")
            final_attempt = context.job.attempts + 1 >= self.max_attempts
            self._service.process(uuid.UUID(version_id), final_attempt=final_attempt)

        self._task = process_document_version

    def bind(self, service: "DocumentService") -> None:
        """Give the task the service it runs. The service is built with `enqueue`, so the
        two are connected after both exist."""
        self._service = service

    def enqueue(self, session: Session, version: DocumentVersion) -> None:
        """Add the job for `version` in the session's transaction."""
        connection = session.connection().connection.driver_connection
        self._task.configure(connection=connection).defer(version_id=str(version.id))
