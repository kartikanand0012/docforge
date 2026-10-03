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
    from docforge.search.service import SearchService
    from docforge.webhooks import WebhookService

logger = logging.getLogger(__name__)

QUEUE_NAME = "extract"
TASK_NAME = "process_document_version"
WEBHOOK_QUEUE = "webhooks"
WEBHOOK_TASK = "deliver_webhook"
INDEX_QUEUE = "index"
INDEX_TASK = "index_document_version"
QUEUES = [QUEUE_NAME, WEBHOOK_QUEUE, INDEX_QUEUE]


class DeliveryNotDone(Exception):
    """The receiver did not accept the event yet: the queue tries again later."""


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
        self._webhooks: WebhookService | None = None

        # Waits of 30 s, then 1.5, 2.5, 3.5 ... minutes (30 s + 60 s per attempt): about half
        # an hour over eight attempts. The service counts attempts and marks the delivery
        # failed when they run out, so the queue allows a few more than it needs.
        @self.app.task(
            name=WEBHOOK_TASK,
            queue=WEBHOOK_QUEUE,
            retry=procrastinate.RetryStrategy(
                max_attempts=12, wait=30, linear_wait=60, retry_exceptions={DeliveryNotDone}
            ),
        )
        def deliver_webhook(delivery_id: str, tenant_id: str) -> None:
            if self._webhooks is None:
                raise RuntimeError("the queue is not bound to a webhook service")
            outcome = self._webhooks.deliver(uuid.UUID(delivery_id), uuid.UUID(tenant_id))
            if outcome == "retry":
                raise DeliveryNotDone(delivery_id)

        self._deliver = deliver_webhook
        self._search: SearchService | None = None

        @self.app.task(
            name=INDEX_TASK,
            queue=INDEX_QUEUE,
            retry=procrastinate.RetryStrategy(max_attempts=5, wait=30, linear_wait=60),
        )
        def index_document_version(version_id: str) -> None:
            if self._search is None:
                logger.warning("no search service: version %s is not indexed", version_id)
                return
            try:
                self._search.index_version(uuid.UUID(version_id))
            except DocumentNotFound:
                logger.error("dropping index job for version %s: it does not exist", version_id)

        self._index = index_document_version

    def bind(self, service: "DocumentService") -> None:
        """Give the task the service it runs. The service is built with `enqueue`, so the
        two are connected after both exist."""
        self._service = service

    def bind_search(self, search: "SearchService") -> None:
        self._search = search

    def defer_index(self, session: Session, version: DocumentVersion) -> None:
        """Queue the search indexing of `version` in the session's transaction."""
        connection = session.connection().connection.driver_connection
        self._index.configure(connection=connection).defer(version_id=str(version.id))

    def bind_webhooks(self, webhooks: "WebhookService") -> None:
        self._webhooks = webhooks

    def defer_delivery(
        self, session: Session, delivery_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        """Queue one webhook delivery in the session's transaction."""
        connection = session.connection().connection.driver_connection
        self._deliver.configure(connection=connection).defer(
            delivery_id=str(delivery_id), tenant_id=str(tenant_id)
        )

    def enqueue(self, session: Session, version: DocumentVersion) -> None:
        """Add the job for `version` in the session's transaction."""
        connection = session.connection().connection.driver_connection
        self._task.configure(connection=connection).defer(version_id=str(version.id))
