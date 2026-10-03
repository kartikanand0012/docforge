"""Search: documents indexed in chunks, found by words and by meaning, cited, per tenant."""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.parsing.cache import CachingParser
from docforge.search.embeddings import FakeEmbedder
from docforge.search.service import Mode, SearchService
from fakes import signed_in
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded" / "parsed"


@pytest.fixture
def indexed(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> tuple[World, SearchService, uuid.UUID, uuid.UUID]:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    # The real parse of the invoice (recorded), so its table rows are cells, as in use.
    world.invoice_parsed = CachingParser(RECORDED).parse(world.invoice_pdf)
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
    indexed: tuple[World, SearchService, uuid.UUID, uuid.UUID], mode: Mode
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


def test_a_processed_document_is_queued_for_indexing_with_its_extraction(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> None:
    queued: list[uuid.UUID] = []
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.index = lambda session, version: queued.append(version.id)

    world.process("invoice")

    assert len(queued) == 1


def test_the_index_job_runs_through_the_real_queue(
    sessions: SessionFactory,
    engine: Engine,
    owner_engine: Engine,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> None:
    import asyncio

    from docforge.queue import JobQueue
    from docforge.worker import run_worker

    queue = JobQueue(engine.url)
    search = SearchService(sessions, FakeEmbedder())
    queue.bind_search(search)
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.index = queue.defer_index

    world.process("invoice")
    asyncio.run(run_worker(queue, wait=False))

    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM chunks")).scalar_one() > 0


def test_a_question_naming_a_code_finds_every_document_that_prints_it_first(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> None:
    """Generic words ("purchase order") must not outrank the code the question names."""
    search = SearchService(sessions, FakeEmbedder())
    documents: dict[str, uuid.UUID] = {}
    for pair in [f"pair_{n:03d}" for n in range(1, 11)]:  # as many as one organisation in the eval
        world = World(sessions, raw_invoice_from_label, raw_order_from_label, pair)
        documents[f"{pair}/po"] = world.process("purchase_order")
        documents[f"{pair}/invoice"] = world.process("invoice")
        if pair == "pair_001":
            po_no = world.order_raw["po_no"]["text"]
    for document_id in documents.values():
        search.index_document(DEFAULT_TENANT_ID, document_id)

    hits = search.search(DEFAULT_TENANT_ID, f"purchase order {po_no}", mode="keyword")

    first_two = {hit.document_id for hit in hits[:2]}
    assert first_two == {documents["pair_001/po"], documents["pair_001/invoice"]}


def test_a_number_written_with_slashes_is_found_by_its_parts(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> None:
    """Full-text parsing reads NVM/26-27/32001 as one path-like word; questions split it."""
    search = SearchService(sessions, FakeEmbedder())
    invoices: dict[str, uuid.UUID] = {}
    numbers: dict[str, str] = {}
    for pair in [f"pair_{n:03d}" for n in range(1, 6)]:
        world = World(sessions, raw_invoice_from_label, raw_order_from_label, pair)
        invoices[pair] = world.process("invoice")
        numbers[pair] = world.invoice_raw["invoice_no"]["text"]
        search.index_document(DEFAULT_TENANT_ID, invoices[pair])
    assert "/" in numbers["pair_003"]

    hits = search.search(DEFAULT_TENANT_ID, f"invoice number {numbers['pair_003']}", mode="keyword")

    assert hits and hits[0].document_id == invoices["pair_003"]


def test_in_hybrid_search_a_named_code_outweighs_a_merely_similar_document(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> None:
    from docforge.search.embeddings import FakeEmbedder as Base

    class OrdersLookAlike(Base):
        """Embeds every purchase order as the best match for any question about one."""

        def embed(self, texts: list[str], task: str) -> list[list[float]]:
            return super().embed(
                [
                    "purchase order"
                    if "Purchase order" in t or "purchase order" in t.lower()[:40]
                    else t
                    for t in texts
                ],
                task,
            )

    search = SearchService(sessions, OrdersLookAlike())
    documents: dict[str, uuid.UUID] = {}
    for pair in [f"pair_{n:03d}" for n in range(1, 6)]:
        world = World(sessions, raw_invoice_from_label, raw_order_from_label, pair)
        documents[f"{pair}/po"] = world.process("purchase_order")
        documents[f"{pair}/invoice"] = world.process("invoice")
        if pair == "pair_001":
            po_no = world.order_raw["po_no"]["text"]
    for document_id in documents.values():
        search.index_document(DEFAULT_TENANT_ID, document_id)

    hits = search.search(DEFAULT_TENANT_ID, f"purchase order {po_no}", mode="hybrid")

    found: list[uuid.UUID] = []
    for hit in hits:
        if hit.document_id not in found:
            found.append(hit.document_id)
    assert {documents["pair_001/po"], documents["pair_001/invoice"]} <= set(found[:5])
