"""The webhook API: administrators only, the secret once, a test event on demand."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docforge.api.app import create_app
from docforge.auth import Authenticator
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.webhooks import WebhookService
from fakes import signed_in

pytestmark = pytest.mark.integration


@pytest.fixture
def deferred() -> list[Any]:
    return []


@pytest.fixture
def hooks(sessions: SessionFactory, deferred: list[Any]) -> WebhookService:
    return WebhookService(
        sessions,
        b"k" * 32,
        lambda s, d, t: deferred.append((d, t)),
        allow_http=True,
        allow_private=True,
    )


@pytest.fixture
def admin(hooks: WebhookService) -> Iterator[TestClient]:
    yield TestClient(signed_in(create_app(None, webhooks=hooks), role="admin"))


def test_an_administrator_adds_a_webhook_and_sees_its_secret_once(
    admin: TestClient, hooks: WebhookService
) -> None:
    created = admin.post(
        "/v1/webhooks", json={"url": "http://127.0.0.1:9/hooks", "events": ["review.signed"]}
    )

    assert created.status_code == 201
    body = created.json()
    assert body["secret"].startswith("whsec_")
    listed = admin.get("/v1/webhooks").json()
    assert [hook["id"] for hook in listed] == [body["id"]]
    assert "secret" not in listed[0]


def test_a_test_event_is_queued_for_delivery(admin: TestClient, deferred: list[Any]) -> None:
    hook_id = admin.post(
        "/v1/webhooks", json={"url": "http://127.0.0.1:9/hooks", "events": ["webhook.test"]}
    ).json()["id"]

    response = admin.post(f"/v1/webhooks/{hook_id}/test")

    assert response.status_code == 202
    assert len(deferred) == 1
    (delivery,) = admin.get(f"/v1/webhooks/{hook_id}/deliveries").json()
    assert (delivery["event_type"], delivery["status"]) == ("webhook.test", "pending")


def test_bad_webhooks_are_refused_with_a_reason(
    admin: TestClient, sessions: SessionFactory
) -> None:
    unknown_event = admin.post(
        "/v1/webhooks", json={"url": "http://127.0.0.1:9/hooks", "events": ["everything"]}
    )
    strict = TestClient(
        signed_in(create_app(None, webhooks=WebhookService(sessions, b"k" * 32, lambda *a: None)))
    )
    internal = strict.post(
        "/v1/webhooks", json={"url": "https://127.0.0.1/hooks", "events": ["review.signed"]}
    )

    assert unknown_event.status_code == 422
    assert internal.status_code == 422 and "public address" in internal.json()["detail"]


def test_a_webhook_is_removed_and_another_tenants_cannot_be(
    admin: TestClient, hooks: WebhookService, other_tenant: uuid.UUID
) -> None:
    hook_id = admin.post(
        "/v1/webhooks", json={"url": "http://127.0.0.1:9/hooks", "events": ["review.signed"]}
    ).json()["id"]
    theirs = TestClient(signed_in(create_app(None, webhooks=hooks), tenant_id=other_tenant))

    assert theirs.delete(f"/v1/webhooks/{hook_id}").status_code == 404
    assert admin.delete(f"/v1/webhooks/{hook_id}").status_code == 204
    assert admin.get("/v1/webhooks").json()[0]["active"] is False


def test_only_administrators_manage_webhooks(
    hooks: WebhookService, sessions: SessionFactory
) -> None:
    for role in ("integrator", "reviewer"):
        client = TestClient(signed_in(create_app(None, webhooks=hooks), role=role))
        assert client.get("/v1/webhooks").status_code == 403
    anonymous = TestClient(create_app(None, webhooks=hooks, authenticator=Authenticator(sessions)))
    assert anonymous.get("/v1/webhooks").status_code == 401
    assert DEFAULT_TENANT_ID
