"""Webhooks: signed deliveries, retried with the same event id, never sent twice."""

import hashlib
import hmac
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from sqlalchemy import select

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import WebhookDelivery
from docforge.db.session import SessionFactory
from docforge.webhooks import UnsafeDestination, WebhookService

pytestmark = pytest.mark.integration

KEY = b"test-signing-key-" + b"0" * 15


class Receiver:
    """A local HTTP endpoint that answers with the statuses it is given, in order."""

    def __init__(self) -> None:
        self.statuses: list[int] = []
        self.received: list[tuple[dict[str, str], bytes]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                receiver.received.append(({k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(receiver.statuses.pop(0) if receiver.statuses else 200)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hooks"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    receiver = Receiver()
    yield receiver
    receiver.server.shutdown()


@pytest.fixture
def deferred() -> list[tuple[uuid.UUID, uuid.UUID]]:
    return []


@pytest.fixture
def hooks(sessions: SessionFactory, deferred: list[tuple[uuid.UUID, uuid.UUID]]) -> WebhookService:
    return WebhookService(
        sessions,
        KEY,
        lambda session, delivery_id, tenant_id: deferred.append((delivery_id, tenant_id)),
        allow_http=True,
        allow_private=True,  # the receiver is on this machine
        max_attempts=3,
    )


def emit(
    hooks: WebhookService, sessions: SessionFactory, event_id: uuid.UUID | None = None
) -> uuid.UUID:
    event_id = event_id or uuid.uuid4()
    with sessions.begin() as session:
        hooks.emit(
            session, DEFAULT_TENANT_ID, "review.signed", {"document_id": "d1"}, event_id=event_id
        )
    return event_id


def test_a_delivery_is_signed_and_names_its_event(
    hooks: WebhookService, sessions: SessionFactory, receiver: Receiver, deferred: list[Any]
) -> None:
    hook_id, secret = hooks.create(
        DEFAULT_TENANT_ID, url=receiver.url, events=["review.signed"], created_by="test"
    )
    event_id = emit(hooks, sessions)

    ((delivery_id, tenant_id),) = deferred
    assert hooks.deliver(delivery_id, tenant_id) == "delivered"

    ((headers, body),) = receiver.received
    payload = json.loads(body)
    assert payload["id"] == str(event_id) and payload["type"] == "review.signed"
    assert payload["data"] == {"document_id": "d1"}
    assert headers["docforge-event-id"] == str(event_id)
    timestamp, signature = (
        part.split("=", 1)[1] for part in headers["docforge-signature"].split(",")
    )
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(signature, expected)
    assert hook_id


def test_a_failed_delivery_is_retried_with_the_same_event_id_and_sent_once_it_succeeds(
    hooks: WebhookService, sessions: SessionFactory, receiver: Receiver, deferred: list[Any]
) -> None:
    hooks.create(DEFAULT_TENANT_ID, url=receiver.url, events=["review.signed"], created_by="test")
    emit(hooks, sessions)
    ((delivery_id, tenant_id),) = deferred
    receiver.statuses = [500, 503]

    outcomes = [hooks.deliver(delivery_id, tenant_id) for _ in range(4)]

    assert outcomes == ["retry", "retry", "delivered", "skipped"]  # the last finds it done
    ids = {headers["docforge-event-id"] for headers, _ in receiver.received}
    assert len(receiver.received) == 3 and len(ids) == 1
    with sessions() as session:
        delivery = session.get_one(WebhookDelivery, delivery_id)
    assert (delivery.status, delivery.attempts, delivery.last_status) == ("delivered", 3, 200)


def test_the_same_event_emitted_twice_is_delivered_once(
    hooks: WebhookService, sessions: SessionFactory, receiver: Receiver, deferred: list[Any]
) -> None:
    hooks.create(DEFAULT_TENANT_ID, url=receiver.url, events=["review.signed"], created_by="test")
    event_id = uuid.uuid4()

    emit(hooks, sessions, event_id)
    emit(hooks, sessions, event_id)

    assert len(deferred) == 1
    with sessions() as session:
        assert len(session.scalars(select(WebhookDelivery)).all()) == 1


def test_a_delivery_that_keeps_failing_stops_after_the_last_attempt(
    hooks: WebhookService, sessions: SessionFactory, receiver: Receiver, deferred: list[Any]
) -> None:
    hooks.create(DEFAULT_TENANT_ID, url=receiver.url, events=["review.signed"], created_by="test")
    emit(hooks, sessions)
    ((delivery_id, tenant_id),) = deferred
    receiver.statuses = [500] * 10

    outcomes = [hooks.deliver(delivery_id, tenant_id) for _ in range(4)]

    assert outcomes == ["retry", "retry", "failed", "skipped"]
    assert len(receiver.received) == 3


def test_only_subscribed_and_active_webhooks_get_an_event(
    hooks: WebhookService, sessions: SessionFactory, receiver: Receiver, deferred: list[Any]
) -> None:
    hooks.create(
        DEFAULT_TENANT_ID, url=receiver.url, events=["document.processed"], created_by="test"
    )
    gone, _ = hooks.create(
        DEFAULT_TENANT_ID, url=receiver.url, events=["review.signed"], created_by="test"
    )
    hooks.delete(DEFAULT_TENANT_ID, gone, actor="test")

    emit(hooks, sessions)

    assert deferred == []


def test_an_unsafe_destination_is_refused_when_the_webhook_is_made(
    sessions: SessionFactory,
) -> None:
    strict = WebhookService(sessions, KEY, lambda *args: None)

    with pytest.raises(UnsafeDestination):
        strict.create(
            DEFAULT_TENANT_ID,
            url="http://127.0.0.1:1/hooks",
            events=["review.signed"],
            created_by="test",
        )


def test_a_destination_that_became_unsafe_is_not_called(
    hooks: WebhookService,
    sessions: SessionFactory,
    receiver: Receiver,
    deferred: list[Any],
) -> None:
    """Checked again at delivery: a name can be pointed at an internal address later."""
    hooks.create(DEFAULT_TENANT_ID, url=receiver.url, events=["review.signed"], created_by="test")
    emit(hooks, sessions)
    ((delivery_id, tenant_id),) = deferred
    strict = WebhookService(sessions, KEY, lambda *args: None, max_attempts=1)

    assert strict.deliver(delivery_id, tenant_id) == "failed"
    assert receiver.received == []


def test_another_tenants_webhooks_and_deliveries_are_invisible(
    hooks: WebhookService, sessions: SessionFactory, receiver: Receiver, other_tenant: uuid.UUID
) -> None:
    hook_id, _ = hooks.create(
        DEFAULT_TENANT_ID, url=receiver.url, events=["review.signed"], created_by="test"
    )

    assert hooks.webhooks(other_tenant) == []
    with pytest.raises(LookupError):
        hooks.deliveries(other_tenant, hook_id)
    with pytest.raises(LookupError):
        hooks.delete(other_tenant, hook_id, actor="test")


def test_the_secret_is_shown_once_and_not_stored(
    hooks: WebhookService, sessions: SessionFactory, receiver: Receiver, owner_engine: Any
) -> None:
    from sqlalchemy import text

    _, secret = hooks.create(
        DEFAULT_TENANT_ID, url=receiver.url, events=["review.signed"], created_by="test"
    )

    with owner_engine.connect() as conn:
        stored = " ".join(str(row) for row in conn.execute(text("SELECT * FROM webhooks")))
    assert secret not in stored
    assert all(secret not in str(hook) for hook in hooks.webhooks(DEFAULT_TENANT_ID))
    assert all("secret" not in hook for hook in hooks.webhooks(DEFAULT_TENANT_ID))


Factory = Callable[..., Any]


def test_processing_a_document_and_signing_its_review_emit_events(
    sessions: SessionFactory,
    raw_invoice_from_label: Factory,
    raw_order_from_label: Factory,
    deferred: list[Any],
    receiver: Receiver,
) -> None:
    from docforge.extraction.pipeline import INVOICE_SPEC
    from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
    from docforge.review.service import ReviewService
    from worlds import World

    hooks = WebhookService(
        sessions, KEY, lambda s, d, t: deferred.append((d, t)), allow_http=True, allow_private=True
    )
    hooks.create(
        DEFAULT_TENANT_ID,
        url=receiver.url,
        events=["document.processed", "review.signed"],
        created_by="test",
    )
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001", events=hooks)
    review = ReviewService(
        sessions,
        world.store,
        {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC},
        events=hooks,
    )
    review.add_reviewer(DEFAULT_TENANT_ID, name="Asha Rao", email="asha@example.com", pin="482913")

    world.process("purchase_order")
    invoice_id = world.process("invoice")
    seen = review.detail(DEFAULT_TENANT_ID, invoice_id).record_sha256
    review.sign(
        DEFAULT_TENANT_ID,
        invoice_id,
        outcome="approved",
        meaning="I approve this invoice for payment",
        reason="ok",
        override_reason=None,
        expected_record_sha256=seen,
        email="asha@example.com",
        pin="482913",
    )

    for delivery_id, tenant_id in deferred:
        assert hooks.deliver(delivery_id, tenant_id) == "delivered"
    events = [json.loads(body) for _, body in receiver.received]
    assert [e["type"] for e in events] == [
        "document.processed",
        "document.processed",
        "review.signed",
    ]
    processed = events[1]["data"]
    assert processed["document_id"] == str(invoice_id) and processed["doc_type"] == "invoice"
    signed = events[2]["data"]
    assert signed["outcome"] == "approved" and signed["draft"]["type"] == "payment_approval_draft"
    assert signed["record_sha256"] == signed["draft"]["record_sha256"]


def test_a_delivery_goes_through_the_real_queue_and_worker(
    sessions: SessionFactory, engine: Any, receiver: Receiver
) -> None:
    """Deferred in the emitting transaction, picked up by a worker running as the app role."""
    import asyncio

    from docforge.queue import JobQueue
    from docforge.worker import run_worker

    queue = JobQueue(engine.url)
    hooks = WebhookService(sessions, KEY, queue.defer_delivery, allow_http=True, allow_private=True)
    queue.bind_webhooks(hooks)
    hooks.create(DEFAULT_TENANT_ID, url=receiver.url, events=["review.signed"], created_by="test")
    receiver.statuses = [500]  # the first attempt fails; the queue tries again

    emit(hooks, sessions)
    asyncio.run(run_worker(queue, wait=False))
    asyncio.run(run_worker(queue, wait=False))

    assert len(receiver.received) >= 1
    with sessions() as session:
        (delivery,) = session.scalars(select(WebhookDelivery)).all()
    assert delivery.attempts == len(receiver.received)
    if delivery.status != "delivered":  # the retry waits; it is scheduled, not lost
        assert delivery.status == "pending"


def test_a_delivery_connects_to_the_address_that_was_checked(
    sessions: SessionFactory,
    receiver: Receiver,
    deferred: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name is resolved once; the request goes to that address with the name as Host."""
    import socket

    real = socket.getaddrinfo
    hooks = WebhookService(
        sessions, KEY, lambda s, d, t: deferred.append((d, t)), allow_http=True, allow_private=True
    )
    port = receiver.url.split(":")[2].split("/")[0]
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: real("127.0.0.1" if host == "hooks.test" else host, *a, **k),
    )
    hooks.create(
        DEFAULT_TENANT_ID,
        url=f"http://hooks.test:{port}/hooks",
        events=["review.signed"],
        created_by="test",
    )
    emit(hooks, sessions)
    ((delivery_id, tenant_id),) = deferred
    lookups: list[str] = []

    def counting(host: str, *args: Any, **kwargs: Any) -> Any:
        lookups.append(host)
        return real("127.0.0.1" if host == "hooks.test" else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", counting)

    assert hooks.deliver(delivery_id, tenant_id) == "delivered"
    ((headers, _),) = receiver.received
    assert headers["host"] == f"hooks.test:{port}"
    assert lookups.count("hooks.test") == 1  # the name resolved once, for the check


def test_no_database_lock_is_held_while_the_receiver_answers(
    sessions: SessionFactory, deferred: list[Any], owner_engine: Any
) -> None:
    """A slow receiver must not hold a row lock or a transaction open."""
    from sqlalchemy import text

    seen: list[str] = []

    class Probe:
        def __enter__(self) -> "Probe":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: Any) -> Any:
            import contextlib

            import httpx

            with owner_engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '200ms'"))
                conn.execute(text("SELECT 1 FROM webhook_deliveries FOR UPDATE NOWAIT"))
                seen.append("row free")
            return contextlib.nullcontext(httpx.Response(200))

    hooks = WebhookService(
        sessions,
        KEY,
        lambda s, d, t: deferred.append((d, t)),
        allow_http=True,
        allow_private=True,
        client=lambda: Probe(),  # type: ignore[arg-type,return-value]
    )
    hooks.create(
        DEFAULT_TENANT_ID,
        url="http://127.0.0.1:9/hooks",
        events=["review.signed"],
        created_by="test",
    )
    emit(hooks, sessions)
    ((delivery_id, tenant_id),) = deferred

    assert hooks.deliver(delivery_id, tenant_id) == "delivered"
    assert seen == ["row free"]
