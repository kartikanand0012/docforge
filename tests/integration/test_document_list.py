"""Every document of an organisation, newest first, with its stage: where an uploader finds
a document again once it has left the review queue."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docforge.api.app import create_app
from docforge.db.session import SessionFactory
from fakes import signed_in
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.fixture
def world(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> World:
    return World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")


def test_documents_are_listed_newest_first_with_their_stage(world: World) -> None:
    order_id = world.process("purchase_order")
    invoice_id = world.process("invoice")
    assert world.service is not None
    client = TestClient(signed_in(create_app(None, service=world.service), role="reviewer"))

    body = client.get("/v1/documents").json()

    assert [item["id"] for item in body["items"]] == [str(invoice_id), str(order_id)]
    assert body["items"][0]["stage"] == "processed"
    assert body["items"][0]["doc_type"] == "invoice"
    assert body["next_before"] is None


def test_the_list_pages_and_filters(world: World) -> None:
    order_id = world.process("purchase_order")
    invoice_id = world.process("invoice")
    assert world.service is not None
    client = TestClient(signed_in(create_app(None, service=world.service), role="reviewer"))

    first = client.get("/v1/documents", params={"limit": 1}).json()
    second = client.get("/v1/documents", params={"limit": 1, "before": first["next_before"]}).json()
    orders = client.get("/v1/documents", params={"doc_type": "purchase_order"}).json()
    failed = client.get("/v1/documents", params={"stage": "failed"}).json()

    assert [i["id"] for i in first["items"]] == [str(invoice_id)]
    assert [i["id"] for i in second["items"]] == [str(order_id)]
    assert [i["id"] for i in orders["items"]] == [str(order_id)]
    assert failed["items"] == []


def test_another_organisation_sees_none_of_them(world: World, other_tenant: uuid.UUID) -> None:
    world.process("invoice")
    assert world.service is not None
    client = TestClient(signed_in(create_app(None, service=world.service), tenant_id=other_tenant))

    assert client.get("/v1/documents").json()["items"] == []


def test_a_bad_cursor_or_limit_is_refused(world: World) -> None:
    assert world.build() is not None
    client = TestClient(signed_in(create_app(None, service=world.service), role="reviewer"))

    assert client.get("/v1/documents", params={"before": "not-a-cursor"}).status_code == 422
    assert client.get("/v1/documents", params={"limit": 0}).status_code == 422
    assert client.get("/v1/documents", params={"limit": 501}).status_code == 422


@pytest.mark.parametrize(
    "raw",
    [
        "0001-01-01T00:00:00+23:59|00000000-0000-0000-0000-000000000000",
        "2026-10-04T10:00:00|00000000-0000-0000-0000-000000000000",  # no time zone
    ],
)
def test_a_cursor_with_an_impossible_time_is_refused_not_an_error(world: World, raw: str) -> None:
    import base64

    assert world.build() is not None
    client = TestClient(
        signed_in(create_app(None, service=world.service), role="reviewer"),
        raise_server_exceptions=False,
    )
    cursor = base64.urlsafe_b64encode(raw.encode()).decode()

    assert client.get("/v1/documents", params={"before": cursor}).status_code == 422
