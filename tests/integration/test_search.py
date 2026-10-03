"""Search: documents indexed in chunks, found by words and by meaning, cited, per tenant."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from docforge.search.service import SearchService
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.search.embeddings import FakeEmbedder
from fakes import signed_in
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.fixture
def indexed(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> tuple[World, SearchService, uuid.UUID, uuid.UUID]:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    search = SearchService(sessions, FakeEmbedder())
    order_id = world.process("purchase_order")
    invoice_id = world.process("invoice")
    for document_id in (order_id, invoice_id):
        search.index_document(DEFAULT_TENANT_ID, document_id)
    return world, search, invoice_id, order_id


def test_a_batch_number_finds_its_invoice_and_the_row_that_prints_it(
    indexed: tuple[World, SearchService, uuid.UUID, uuid.UUID],
) -> None:
    world, search, invoice_id, _ = indexed
    batch = world.invoice_raw["lines"][0]["batch_no"]["text"]

    (top, *_) = search.search(DEFAULT_TENANT_ID, batch, mode="keyword")

    assert top.document_id == invoice_id
    assert top.kind == "table_row" and batch in top.text
    assert top.boxes and top.boxes[0]["page"] == 1  # where to show it on the page


@pytest.mark.parametrize("mode", ["keyword", "vector", "hybrid"])
def test_every_mode_finds_the_document_a_question_is_about(
    indexed: tuple[World, SearchService, uuid.UUID, uuid.UUID], mode: str
) -> None:
    world, search, _, order_id = indexed
    po_no = world.order_raw["po_no"]["text"]

    results = search.search(DEFAULT_TENANT_ID, f"purchase order {po_no}", mode=mode)

    assert order_id in [r.document_id for r in results[:5]]


def test_indexing_the_same_version_twice_adds_nothing(
    indexed: tuple[World, SearchService, uuid.UUID, uuid.UUID], owner_engine: Engine
) -> None:
    _, search, invoice_id, _ = indexed
    with owner_engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM chunks")).scalar_one()

    search.index_document(DEFAULT_TENANT_ID, invoice_id)

    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM chunks")).scalar_one() == before


def test_another_tenant_finds_nothing_of_this_ones(
    indexed: tuple[World, SearchService, uuid.UUID, uuid.UUID], other_tenant: uuid.UUID
) -> None:
    world, search, _, _ = indexed
    batch = world.invoice_raw["lines"][0]["batch_no"]["text"]

    for mode in ("keyword", "vector", "hybrid"):
        assert search.search(other_tenant, batch, mode=mode) == []


def test_chunks_are_protected_by_row_level_security(
    indexed: tuple[World, SearchService, uuid.UUID, uuid.UUID], engine: Engine
) -> None:
    with engine.connect() as conn:  # the application's role, no tenant set
        assert conn.execute(text("SELECT count(*) FROM chunks")).scalar_one() == 0


def test_the_search_api_returns_cited_results(
    indexed: tuple[World, SearchService, uuid.UUID, uuid.UUID],
) -> None:
    world, search, invoice_id, _ = indexed
    client = TestClient(signed_in(create_app(None, search=search), role="integrator"))
    batch = world.invoice_raw["lines"][0]["batch_no"]["text"]

    response = client.get("/v1/search", params={"q": batch, "k": 3})

    assert response.status_code == 200
    top = response.json()["results"][0]
    assert top["document_id"] == str(invoice_id)
    assert top["doc_type"] == "invoice" and top["boxes"]
    assert client.get("/v1/search", params={"q": ""}).status_code == 422
