"""Tracing: each stage of a document's processing, each model call with its tokens and cost,
each search and API request is a span. No document content, question or credential is put on
a span: traces go to a third-party backend."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.extraction.pipeline import ExtractionError, InvoicePipeline
from docforge.parsing.cache import CachingParser
from docforge.search.embeddings import FakeEmbedder
from docforge.search.service import SearchService
from docforge.telemetry import set_prices
from fakes import ScriptedProvider, signed_in
from tracing import EXPORTER
from worlds import World

pytestmark = pytest.mark.integration

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded" / "parsed"
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.fixture
def spans() -> Iterator[Callable[[], list[ReadableSpan]]]:
    EXPORTER.clear()
    set_prices({"fake/fake-1": (0.30, 2.50)})  # the scripted model's
    yield lambda: list(EXPORTER.get_finished_spans())
    set_prices({})
    EXPORTER.clear()


@pytest.fixture
def world(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> World:
    return World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")


def by_name(spans: list[ReadableSpan]) -> dict[str, ReadableSpan]:
    return {span.name: span for span in spans}


def every_value(spans: list[ReadableSpan]) -> str:
    return " ".join(str(v) for span in spans for v in (span.attributes or {}).values())


def test_processing_a_document_traces_each_stage_under_one_span(
    world: World, spans: Callable[[], list[ReadableSpan]]
) -> None:
    world.process("invoice")
    named = by_name(spans())

    root = named["document.process"]
    for stage in ("pipeline.parse", "pipeline.extract", "llm.generate", "pipeline.assess"):
        assert named[stage].context.trace_id == root.context.trace_id, stage
    assert named["llm.generate"].parent is not None
    assert root.attributes is not None
    assert root.attributes["docforge.doc_type"] == "invoice"
    assert root.attributes["docforge.outcome"] == "succeeded"
    assert root.attributes["docforge.tenant_id"] == str(DEFAULT_TENANT_ID)


def test_a_model_call_carries_its_tokens_and_the_document_its_cost(
    world: World, spans: Callable[[], list[ReadableSpan]]
) -> None:
    world.process("invoice")
    named = by_name(spans())

    call = named["llm.generate"].attributes or {}
    assert call["gen_ai.request.model"]
    assert call["gen_ai.usage.input_tokens"] >= 0
    assert call["docforge.prompt_version"] == "invoice-v1"
    root = named["document.process"].attributes or {}
    assert root["docforge.model_calls"] >= 1
    assert root["docforge.cost_usd"] >= 0


def test_no_document_content_is_put_on_a_span(
    world: World, spans: Callable[[], list[ReadableSpan]]
) -> None:
    world.process("invoice")
    values = every_value(spans())

    raw = world.invoice_raw
    for printed in (
        raw["invoice_no"]["text"],
        raw["lines"][0]["batch_no"]["text"],
        raw["seller"]["name"]["text"],
    ):
        assert printed not in values


def test_a_search_is_traced_with_its_mode_and_count_but_not_its_question(
    world: World, sessions: SessionFactory, spans: Callable[[], list[ReadableSpan]]
) -> None:
    search = SearchService(sessions, FakeEmbedder())
    search.index_document(DEFAULT_TENANT_ID, world.process("invoice"))
    question = world.invoice_raw["lines"][0]["batch_no"]["text"]
    EXPORTER.clear()

    hits = search.search(DEFAULT_TENANT_ID, question, mode="hybrid")
    named = by_name(spans())

    attributes = named["search.query"].attributes or {}
    assert attributes["docforge.search.mode"] == "hybrid"
    assert attributes["docforge.search.hits"] == len(hits)
    assert question not in every_value(spans())


def test_an_api_request_is_traced_by_route_without_its_query(
    sessions: SessionFactory, spans: Callable[[], list[ReadableSpan]]
) -> None:
    app = create_app(None, search=SearchService(sessions, FakeEmbedder()))
    client = TestClient(app)

    client.get("/v1/search", params={"q": "secret-question"})
    client.get("/healthz")

    requests = {s.name: s for s in spans() if s.name.startswith("GET ")}
    assert set(requests) >= {"GET /healthz", "GET /v1/search"}
    assert (requests["GET /v1/search"].attributes or {})["http.response.status_code"] == 401
    assert (requests["GET /healthz"].attributes or {})["http.response.status_code"] == 200
    assert "secret-question" not in every_value(spans())


def everything_on(spans: list[ReadableSpan]) -> str:
    """Attributes, events (a recorded exception's message and stack) and status text."""
    parts = [every_value(spans)]
    for span in spans:
        parts += [span.status.description or ""]
        for event in span.events:
            parts += [event.name, *(str(v) for v in (event.attributes or {}).values())]
    return " ".join(parts)


class _FailingEmbedder(FakeEmbedder):
    def embed(self, texts: list[str], task: str) -> list[list[float]]:
        raise RuntimeError(f"embedding failed for {texts}")


def test_a_failing_search_puts_the_error_type_on_its_span_but_not_the_question(
    sessions: SessionFactory, spans: Callable[[], list[ReadableSpan]]
) -> None:
    search = SearchService(sessions, _FailingEmbedder())

    with pytest.raises(RuntimeError):
        search.search(DEFAULT_TENANT_ID, "secret-question", mode="hybrid")

    named = by_name(spans())
    assert (named["search.query"].attributes or {})["docforge.error"] == "RuntimeError"
    assert "secret-question" not in everything_on(spans())


def test_a_malformed_model_reply_does_not_reach_a_span(
    world: World, spans: Callable[[], list[ReadableSpan]]
) -> None:
    parsed = CachingParser(RECORDED).parse(world.invoice_pdf)
    pipeline = InvoicePipeline(
        CachingParser(RECORDED), ScriptedProvider(['{"invoice_no": "SECRET-VALUE-42"}'] * 2)
    )

    with pytest.raises(ExtractionError):
        pipeline.extract(parsed)

    assert "SECRET-VALUE-42" not in everything_on(spans())
    assert (by_name(spans())["pipeline.extract"].attributes or {})["docforge.error"] == (
        "ExtractionError"
    )


def test_a_request_that_fails_inside_still_has_its_status_on_the_span(
    sessions: SessionFactory, spans: Callable[[], list[ReadableSpan]]
) -> None:
    app = signed_in(create_app(None, search=SearchService(sessions, _FailingEmbedder())))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/v1/search", params={"q": "secret-question"})

    assert response.status_code == 500
    request = by_name(spans())["GET /v1/search"]
    assert (request.attributes or {})["http.response.status_code"] == 500
    assert (request.attributes or {})["docforge.error"] == "RuntimeError"
    assert "secret-question" not in everything_on(spans())


def test_a_document_that_fails_has_its_outcome_on_the_span(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    spans: Callable[[], list[ReadableSpan]],
) -> None:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.invoice_raw = {"lines": "not a list"}  # a reply that does not fit the schema
    service = world.build()
    ingested = service.ingest(
        tenant_id=DEFAULT_TENANT_ID, doc_type="invoice", filename="i.pdf",
        data=world.invoice_pdf, actor="t",
    )  # fmt: skip
    assert ingested.version is not None

    outcome = service.process(ingested.version.id)

    root = by_name(spans())["document.process"]
    assert (root.attributes or {})["docforge.outcome"] == outcome == "failed"
    assert root.status.status_code.name == "ERROR"
