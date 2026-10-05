"""Where a document is, for the person who uploaded it: stored, parsing, extracting, checking,
indexing, ready (ready to chat), or failed with the reason. Read from the audit trail, so the
timeline and the record of what happened can never disagree."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from fakes import signed_in
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


class Events:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def emit(
        self,
        session: Session,
        tenant_id: uuid.UUID,
        event_type: str,
        data: dict[str, Any],
        *,
        event_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        self.sent.append((event_type, data))
        return event_id or uuid.uuid4()


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture
def world(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    events: Events,
) -> World:
    built = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001", events)
    built.index = lambda session, version: None  # indexing is queued, run by hand below
    return built


def stages(world: World, document_id: uuid.UUID) -> list[str]:
    assert world.service is not None
    return [step.stage for step in world.service.timeline(DEFAULT_TENANT_ID, document_id)]


def test_a_document_moves_through_each_stage_to_ready(world: World, events: Events) -> None:
    document_id = world.process("invoice")
    assert world.service is not None

    assert stages(world, document_id) == ["stored", "parsing", "extracting", "checking", "indexing"]
    assert world.service.detail(DEFAULT_TENANT_ID, document_id).document.stage == "indexing"

    version_id = world.service.detail(DEFAULT_TENANT_ID, document_id).versions[-1].id
    world.service.mark_indexed(version_id)
    world.service.mark_indexed(version_id)  # a redelivered index job changes nothing

    assert stages(world, document_id)[-1] == "ready"
    assert world.service.detail(DEFAULT_TENANT_ID, document_id).document.stage == "ready"
    ready = [data for kind, data in events.sent if kind == "document.ready_for_chat"]
    assert ready == [{"document_id": str(document_id), "version_no": 1}]


def test_every_step_has_a_time_in_order(world: World) -> None:
    document_id = world.process("invoice")
    assert world.service is not None

    steps = world.service.timeline(DEFAULT_TENANT_ID, document_id)

    assert [s.at for s in steps] == sorted(s.at for s in steps)


def test_a_document_that_fails_says_so_with_the_reason(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> None:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.invoice_raw = {"lines": "not a list"}  # a reply that never fits the schema
    service = world.build()
    ingested = service.ingest(
        tenant_id=DEFAULT_TENANT_ID, doc_type="invoice", filename="i.pdf",
        data=world.invoice_pdf, actor="t",
    )  # fmt: skip
    assert ingested.version is not None
    service.process(ingested.version.id)

    last = service.timeline(DEFAULT_TENANT_ID, ingested.document.id)[-1]

    assert last.stage == "failed"
    assert last.detail == "The model reply did not fit the schema."
    assert service.detail(DEFAULT_TENANT_ID, ingested.document.id).document.stage == "failed"


def test_without_search_a_document_ends_processed_not_ready(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> None:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    document_id = world.process("invoice")

    assert stages(world, document_id)[-1] == "processed"


def test_the_api_gives_the_stage_the_timeline_and_a_live_stream(world: World) -> None:
    document_id = world.process("invoice")
    assert world.service is not None
    client = TestClient(signed_in(create_app(None, service=world.service), role="integrator"))

    detail = client.get(f"/v1/documents/{document_id}").json()
    timeline = client.get(f"/v1/documents/{document_id}/timeline").json()
    version_id = world.service.detail(DEFAULT_TENANT_ID, document_id).versions[-1].id
    world.service.mark_indexed(version_id)
    with client.stream("GET", f"/v1/documents/{document_id}/events") as stream:
        body = "".join(stream.iter_text())

    assert detail["document"]["stage"] == "indexing"
    assert detail["document"]["ready_for_chat"] is False
    assert [step["stage"] for step in timeline][-1] == "indexing"
    assert "event: stage" in body and '"stage": "ready"' in body
    last = body.strip().split("\n\n")[-1]
    assert '"stage": "ready"' in last  # the stream ends once ready


def test_another_tenant_sees_no_timeline(world: World, other_tenant: uuid.UUID) -> None:
    document_id = world.process("invoice")
    assert world.service is not None
    client = TestClient(signed_in(create_app(None, service=world.service), tenant_id=other_tenant))

    assert client.get(f"/v1/documents/{document_id}/timeline").status_code == 404
    assert client.get(f"/v1/documents/{document_id}/events").status_code == 404


def test_one_caller_cannot_hold_open_unlimited_streams(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each stream polls the database; a cap per caller keeps the API's threads for others.
    A stream that ends gives its place back."""
    from docforge.api import documents as api_documents

    document_id = world.process("invoice")
    assert world.service is not None
    version_id = world.service.detail(DEFAULT_TENANT_ID, document_id).versions[-1].id
    world.service.mark_indexed(version_id)  # ready: each stream ends after the history
    client = TestClient(signed_in(create_app(None, service=world.service), role="integrator"))
    url = f"/v1/documents/{document_id}/events"

    monkeypatch.setattr(api_documents, "MAX_STREAMS_PER_CALLER", 1)
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200  # the first one's place was given back
    assert not api_documents._open_streams

    monkeypatch.setattr(api_documents, "MAX_STREAMS_PER_CALLER", 0)
    refused = client.get(url)
    assert refused.status_code == 429
