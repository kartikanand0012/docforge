"""Webhooks managed from the screen: disable and enable, delete, rotate the secret, send a
test, re-send a failed delivery, and see when the next attempt is due. Every change is in the
hash-chained audit log, with the host only (a URL's path or query can hold a token). The rules
against local and private destinations hold for every action that sends.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import procrastinate
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from test_webhooks import KEY, Receiver, emit

from docforge import audit
from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry
from docforge.db.session import SessionFactory
from docforge.limits import LocalLimits
from docforge.webhooks import (
    EVENTS,
    MAX_WEBHOOKS,
    DeliveryNotFailed,
    TooManyWebhooks,
    UnsafeDestination,
    WebhookDisabled,
    WebhookService,
    next_wait,
    sign,
)
from fakes import signed_in

pytestmark = pytest.mark.integration

ADMIN = "reviewer:00000000-0000-0000-0000-000000000001"
Deferred = list[tuple[uuid.UUID, uuid.UUID]]


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    receiver = Receiver()
    yield receiver
    receiver.server.shutdown()


@pytest.fixture
def deferred() -> Deferred:
    return []


@pytest.fixture
def hooks(sessions: SessionFactory, deferred: Deferred) -> WebhookService:
    return WebhookService(
        sessions,
        KEY,
        lambda session, delivery, tenant: deferred.append((delivery, tenant)),
        allow_http=True,
        allow_private=True,  # the receiver is on this machine
        max_attempts=2,
    )


def actions(owner_sessions: SessionFactory) -> list[tuple[str, dict[str, Any]]]:
    with owner_sessions() as session:
        rows = session.scalars(
            select(AuditEntry).where(AuditEntry.target_type == "webhook").order_by(AuditEntry.id)
        )
        return [(row.action, row.details) for row in rows]


def make(hooks: WebhookService, receiver: Receiver) -> tuple[uuid.UUID, str]:
    url = receiver.url + "?token=abc"  # a receiver's own token, never to be logged
    return hooks.create(DEFAULT_TENANT_ID, url=url, events=["review.signed"], created_by=ADMIN)


def delivery_of(hooks: WebhookService, hook_id: uuid.UUID) -> dict[str, Any]:
    return hooks.deliveries(DEFAULT_TENANT_ID, hook_id).items[0]


# --- the service --------------------------------------------------------------------------------


def test_every_change_is_audited_with_the_host_only_and_a_failed_change_leaves_nothing(
    hooks: WebhookService, receiver: Receiver, owner_sessions: SessionFactory
) -> None:
    hook_id, _ = make(hooks, receiver)
    hooks.set_active(DEFAULT_TENANT_ID, hook_id, False, actor=ADMIN)
    hooks.set_active(DEFAULT_TENANT_ID, hook_id, True, actor=ADMIN)
    hooks.rotate(DEFAULT_TENANT_ID, hook_id, actor=ADMIN)
    hooks.emit_test(DEFAULT_TENANT_ID, hook_id, actor=ADMIN)
    hooks.delete(DEFAULT_TENANT_ID, hook_id, actor=ADMIN)
    with pytest.raises(ValueError):
        hooks.create(DEFAULT_TENANT_ID, url=receiver.url, events=["nope"], created_by=ADMIN)

    logged = actions(owner_sessions)
    assert [a for a, _ in logged] == [
        "webhook.created", "webhook.disabled", "webhook.enabled", "webhook.secret_rotated",
        "webhook.test_sent", "webhook.deleted",
    ]  # fmt: skip
    assert logged[0][1] == {"host": "127.0.0.1", "events": "review.signed"}
    assert all("token" not in str(d) and "/hooks" not in str(d) for _, d in logged)
    assert logged[3][1] == {"secret_version": 2}
    with owner_sessions() as session:
        assert audit.verify_chain(session, DEFAULT_TENANT_ID).consistent


def test_a_rotated_secret_signs_every_later_attempt_and_the_old_one_no_longer_verifies(
    hooks: WebhookService, receiver: Receiver, sessions: SessionFactory, deferred: Deferred
) -> None:
    hook_id, old = make(hooks, receiver)
    emit(hooks, sessions)  # pending: signed when it is sent
    new = hooks.rotate(DEFAULT_TENANT_ID, hook_id, actor=ADMIN)
    assert new != old
    for delivery_id, tenant in deferred:
        hooks.deliver(delivery_id, tenant)

    headers, body = receiver.received[-1]
    stamp = int(headers["docforge-signature"].split(",")[0].removeprefix("t="))
    assert headers["docforge-signature"] == sign(new, body, timestamp=stamp)
    assert headers["docforge-signature"] != sign(old, body, timestamp=stamp)
    listed = next(h for h in hooks.webhooks(DEFAULT_TENANT_ID) if h["id"] == hook_id)
    assert listed["secret_rotated_at"] is not None and "secret" not in listed


def test_enabling_checks_the_destination_again(
    hooks: WebhookService, receiver: Receiver, sessions: SessionFactory
) -> None:
    hook_id, _ = make(hooks, receiver)
    hooks.set_active(DEFAULT_TENANT_ID, hook_id, False, actor=ADMIN)
    strict = WebhookService(sessions, KEY, lambda *a: None, allow_http=True)  # local refused
    with pytest.raises(UnsafeDestination):
        strict.set_active(DEFAULT_TENANT_ID, hook_id, True, actor=ADMIN)


def test_a_pending_delivery_fails_saying_whether_its_webhook_was_disabled_or_removed(
    hooks: WebhookService, receiver: Receiver, sessions: SessionFactory, deferred: Deferred
) -> None:
    disabled, _ = make(hooks, receiver)
    removed, _ = make(hooks, receiver)
    emit(hooks, sessions)
    hooks.set_active(DEFAULT_TENANT_ID, disabled, False, actor=ADMIN)
    hooks.delete(DEFAULT_TENANT_ID, removed, actor=ADMIN)
    for delivery_id, tenant in deferred:
        assert hooks.deliver(delivery_id, tenant) == "failed"
    assert delivery_of(hooks, disabled)["last_error"] == "the webhook was disabled"
    assert delivery_of(hooks, removed)["last_error"] == "the webhook was removed"


def test_a_deleted_webhook_leaves_the_list_cannot_be_enabled_and_keeps_its_deliveries(
    hooks: WebhookService, receiver: Receiver, sessions: SessionFactory
) -> None:
    hook_id, _ = make(hooks, receiver)
    emit(hooks, sessions)
    hooks.delete(DEFAULT_TENANT_ID, hook_id, actor=ADMIN)
    assert hook_id not in {h["id"] for h in hooks.webhooks(DEFAULT_TENANT_ID)}
    with pytest.raises(LookupError):
        hooks.set_active(DEFAULT_TENANT_ID, hook_id, True, actor=ADMIN)
    assert len(hooks.deliveries(DEFAULT_TENANT_ID, hook_id).items) == 1


def test_a_disabled_webhook_is_not_sent_a_test(hooks: WebhookService, receiver: Receiver) -> None:
    hook_id, _ = make(hooks, receiver)
    hooks.set_active(DEFAULT_TENANT_ID, hook_id, False, actor=ADMIN)
    with pytest.raises(WebhookDisabled):
        hooks.emit_test(DEFAULT_TENANT_ID, hook_id, actor=ADMIN)


def test_only_a_failed_delivery_is_sent_again_once_with_its_event_id_and_fresh_attempts(
    hooks: WebhookService,
    receiver: Receiver,
    sessions: SessionFactory,
    deferred: Deferred,
    owner_sessions: SessionFactory,
) -> None:
    hook_id, _ = make(hooks, receiver)
    event = emit(hooks, sessions)
    ((delivery_id, tenant),) = deferred
    with pytest.raises(DeliveryNotFailed):  # still pending
        hooks.resend(DEFAULT_TENANT_ID, hook_id, delivery_id, actor=ADMIN)
    receiver.statuses = [500, 500]
    hooks.deliver(delivery_id, tenant)
    hooks.deliver(delivery_id, tenant)  # max_attempts=2: failed
    deferred.clear()

    assert hooks.resend(DEFAULT_TENANT_ID, hook_id, delivery_id, actor=ADMIN) == event
    with pytest.raises(DeliveryNotFailed):  # a second click: it is pending again
        hooks.resend(DEFAULT_TENANT_ID, hook_id, delivery_id, actor=ADMIN)
    assert deferred == [(delivery_id, tenant)]
    row = delivery_of(hooks, hook_id)
    assert (row["status"], row["attempts"], row["event_id"]) == ("pending", 0, event)
    resent = [d for a, d in actions(owner_sessions) if a == "webhook.delivery_resent"]
    assert resent == [
        {"event_id": str(event), "event_type": "review.signed", "previous_attempts": 2,
         "previous_status": 500},
    ]  # fmt: skip
    hooks.deliver(delivery_id, tenant)
    with pytest.raises(DeliveryNotFailed):  # delivered now
        hooks.resend(DEFAULT_TENANT_ID, hook_id, delivery_id, actor=ADMIN)


def test_a_delivery_from_another_webhook_or_organisation_is_not_found(
    hooks: WebhookService,
    receiver: Receiver,
    sessions: SessionFactory,
    deferred: Deferred,
    other_tenant: uuid.UUID,
) -> None:
    first, _ = make(hooks, receiver)
    second, _ = make(hooks, receiver)
    emit(hooks, sessions)  # one delivery for each
    delivery_id = delivery_of(hooks, first)["id"]
    with pytest.raises(LookupError):  # it is the first webhook's, not the second's
        hooks.resend(DEFAULT_TENANT_ID, second, delivery_id, actor=ADMIN)
    with pytest.raises(LookupError):
        hooks.resend(other_tenant, first, delivery_id, actor=ADMIN)
    with pytest.raises(LookupError):
        hooks.rotate(other_tenant, first, actor=ADMIN)


def test_when_the_next_attempt_is_due_is_kept_as_the_queue_will_schedule_it(
    hooks: WebhookService, receiver: Receiver, sessions: SessionFactory, deferred: Deferred
) -> None:
    hook_id, _ = make(hooks, receiver)
    emit(hooks, sessions)
    receiver.statuses = [503]
    ((delivery_id, tenant),) = deferred
    before = datetime.now(UTC)
    assert hooks.deliver(delivery_id, tenant) == "retry"
    row = delivery_of(hooks, hook_id)
    assert row["last_attempt_at"] >= before
    due = row["next_attempt_at"] - row["last_attempt_at"]
    assert abs(due - timedelta(seconds=next_wait(1))) < timedelta(seconds=2)
    listed = next(h for h in hooks.webhooks(DEFAULT_TENANT_ID) if h["id"] == hook_id)
    assert listed["last_delivery"]["last_status"] == 503


def test_the_wait_before_each_attempt_is_the_queues() -> None:
    """The time shown as "next retry" is the time the queue will wait, attempt by attempt."""
    from docforge.queue import WEBHOOK_RETRY, DeliveryNotDone

    assert isinstance(WEBHOOK_RETRY, procrastinate.RetryStrategy)

    class Job:
        def __init__(self, attempts: int) -> None:
            self.attempts = attempts

    for attempt in range(1, 9):
        now = datetime.now(UTC)
        job = Job(attempt - 1)
        decision = WEBHOOK_RETRY.get_retry_decision(exception=DeliveryNotDone(), job=job)  # type: ignore[arg-type]
        assert decision is not None and decision.retry_at is not None
        waited = (decision.retry_at - now).total_seconds()
        assert abs(waited - next_wait(attempt)) < 2, attempt


def test_an_organisation_has_at_most_its_share_of_webhooks(
    hooks: WebhookService, receiver: Receiver
) -> None:
    for _ in range(MAX_WEBHOOKS):
        make(hooks, receiver)
    with pytest.raises(TooManyWebhooks):
        make(hooks, receiver)


def test_document_ready_for_chat_can_be_subscribed_to() -> None:
    assert "document.ready_for_chat" in EVENTS


# --- the API ------------------------------------------------------------------------------------


def api(hooks: WebhookService, role: str = "admin") -> TestClient:
    app = create_app(None, webhooks=hooks, limits=LocalLimits())
    return TestClient(signed_in(app, role=role))


def test_the_api_manages_a_webhook_for_administrators_only(
    hooks: WebhookService, receiver: Receiver
) -> None:
    client = api(hooks)
    made = client.post("/v1/webhooks", json={"url": receiver.url, "events": ["review.signed"]})
    assert made.status_code == 201 and made.headers["cache-control"] == "no-store"
    hook = made.json()["id"]
    assert client.patch(f"/v1/webhooks/{hook}", json={"active": False}).status_code == 200
    assert client.post(f"/v1/webhooks/{hook}/test").status_code == 409  # disabled
    assert client.patch(f"/v1/webhooks/{hook}", json={"active": True}).status_code == 200
    rotated = client.post(f"/v1/webhooks/{hook}/secret")
    assert rotated.status_code == 200 and rotated.headers["cache-control"] == "no-store"
    assert rotated.json()["secret"] != made.json()["secret"]
    assert "document.ready_for_chat" in client.get("/v1/webhooks/events").json()
    listed = client.get("/v1/webhooks").json()
    assert [h["id"] for h in listed] == [hook] and "secret" not in listed[0]
    assert client.delete(f"/v1/webhooks/{hook}").status_code == 204
    assert client.get("/v1/webhooks").json() == []
    reviewer = api(hooks, role="reviewer")
    for method, path in [
        ("get", "/v1/webhooks"),
        ("get", "/v1/webhooks/events"),
        ("patch", f"/v1/webhooks/{hook}"),
        ("post", f"/v1/webhooks/{hook}/secret"),
        ("post", f"/v1/webhooks/{hook}/deliveries/{uuid.uuid4()}/resend"),
    ]:
        assert reviewer.request(method, path, json={"active": True}).status_code == 403, path


def test_sends_from_the_screen_are_limited(hooks: WebhookService, receiver: Receiver) -> None:
    client = api(hooks)
    body = {"url": receiver.url, "events": ["review.signed"]}
    hook = client.post("/v1/webhooks", json=body).json()["id"]
    codes = [client.post(f"/v1/webhooks/{hook}/test").status_code for _ in range(11)]
    assert codes[:10] == [202] * 10 and codes[10] == 429


def test_unknown_or_foreign_ids_are_not_found(hooks: WebhookService) -> None:
    client = api(hooks)
    missing, other = uuid.uuid4(), uuid.uuid4()
    assert client.patch(f"/v1/webhooks/{missing}", json={"active": False}).status_code == 404
    assert client.post(f"/v1/webhooks/{missing}/secret").status_code == 404
    resend = f"/v1/webhooks/{missing}/deliveries/{other}/resend"
    assert client.post(resend).status_code == 404


def test_migration_0024_adds_the_columns_and_valid_indexes(owner_sessions: SessionFactory) -> None:
    with owner_sessions() as session:
        indexes = set(
            session.execute(
                text(
                    "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE i.indisvalid AND c.relname LIKE 'ix_audit_log_%'"
                )
            ).scalars()
        )
        columns = set(
            session.execute(
                text(
                    "SELECT table_name || '.' || column_name FROM information_schema.columns "
                    "WHERE table_name IN ('webhooks', 'webhook_deliveries')"
                )
            ).scalars()
        )
    assert {"ix_audit_log_action", "ix_audit_log_actor", "ix_audit_log_occurred"} <= indexes
    assert {
        "webhooks.deleted_at",
        "webhooks.secret_rotated_at",
        "webhook_deliveries.last_attempt_at",
        "webhook_deliveries.next_attempt_at",
    } <= columns


# --- review findings ----------------------------------------------------------------------------


def test_two_makes_at_once_cannot_pass_the_cap(hooks: WebhookService, receiver: Receiver) -> None:
    import threading

    for _ in range(MAX_WEBHOOKS - 1):
        make(hooks, receiver)
    outcomes: list[str] = []

    def one() -> None:
        try:
            make(hooks, receiver)
            outcomes.append("made")
        except TooManyWebhooks:
            outcomes.append("refused")

    threads = [threading.Thread(target=one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["made", "refused"]
    assert len(hooks.webhooks(DEFAULT_TENANT_ID)) == MAX_WEBHOOKS


def test_an_unsafe_destination_is_refused_without_saying_what_it_resolves_to(
    hooks: WebhookService, sessions: SessionFactory
) -> None:
    """An administrator must not be able to map the server's network by trying names."""
    strict = WebhookService(sessions, KEY, lambda *a: None)
    client = TestClient(signed_in(create_app(None, webhooks=strict, limits=LocalLimits())))
    made = client.post(
        "/v1/webhooks", json={"url": "https://127.0.0.1/hooks", "events": ["review.signed"]}
    )
    assert made.status_code == 422
    assert "127.0.0.1" not in made.text and "resolves" not in made.text


def test_an_unknown_delivery_is_named_as_such(hooks: WebhookService, receiver: Receiver) -> None:
    hook_id, _ = make(hooks, receiver)
    client = api(hooks)
    missing = client.post(f"/v1/webhooks/{hook_id}/deliveries/{uuid.uuid4()}/resend")
    assert missing.status_code == 404 and missing.json()["detail"] == "No such delivery."
    bad_cursor = client.get(
        f"/v1/webhooks/{hook_id}/deliveries", params={"before": str(uuid.uuid4())}
    )
    assert bad_cursor.status_code == 404 and bad_cursor.json()["detail"] == "No such delivery."


def test_making_and_enabling_are_limited_too(hooks: WebhookService, receiver: Receiver) -> None:
    client = api(hooks)
    body = {"url": receiver.url, "events": ["review.signed"]}
    hook = client.post("/v1/webhooks", json=body).json()["id"]
    codes = [
        client.patch(f"/v1/webhooks/{hook}", json={"active": n % 2 == 1}).status_code
        for n in range(30)
    ]
    assert 429 in codes


def test_changes_are_recorded_under_the_one_who_made_them(
    hooks: WebhookService, receiver: Receiver, owner_sessions: SessionFactory
) -> None:
    import inspect

    for method in (hooks.create, hooks.emit_test, hooks.delete, hooks.rotate, hooks.set_active):
        parameters = inspect.signature(method).parameters
        who = parameters.get("actor") or parameters.get("created_by")
        assert who is not None and who.default is inspect.Parameter.empty, method.__name__
    assert not hasattr(hooks, "deactivate")


def test_nothing_secret_is_ever_written_to_the_log(
    hooks: WebhookService, receiver: Receiver, sessions: SessionFactory,
    deferred: Deferred, owner_sessions: SessionFactory,
) -> None:  # fmt: skip
    """Whatever the screen shows, the log itself keeps every detail, hashed: a secret, a URL's
    path or token, a key's token must never reach it."""
    from docforge.auth import Authenticator

    token = Authenticator(sessions).create_api_key(DEFAULT_TENANT_ID, name="erp", role="integrator")
    hook_id, first = make(hooks, receiver)
    second = hooks.rotate(DEFAULT_TENANT_ID, hook_id, actor=ADMIN)
    emit(hooks, sessions)
    receiver.statuses = [500, 500]
    ((delivery_id, tenant),) = deferred
    hooks.deliver(delivery_id, tenant)
    hooks.deliver(delivery_id, tenant)
    hooks.resend(DEFAULT_TENANT_ID, hook_id, delivery_id, actor=ADMIN)
    Authenticator(sessions).revoke_api_key(DEFAULT_TENANT_ID, token)
    with owner_sessions() as session:
        logged = " ".join(
            str(row) for row in session.execute(text("SELECT actor, details::text FROM audit_log"))
        )
    for secret in (first, second, token, token.split("_", 2)[2], "/hooks", "token=abc"):
        assert secret not in logged, secret
