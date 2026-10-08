"""A simulated model, for robustness and capacity runs on a stack with the real parser and
converter: valid empty replies of any schema, after a model time chosen for the run. It is
switched on only by name, refuses to start otherwise, and says it is on wherever an operator
looks."""

import json
import random

import pytest

from docforge.chat.service import RawAnswer
from docforge.config import Settings
from docforge.extraction.coa import RawCoa
from docforge.extraction.purchase_order import RawPurchaseOrder
from docforge.extraction.schema import RawInvoice
from docforge.llm.base import LLMRequest
from docforge.loadtest.simulated import SimulatedProvider, minimal_reply, model_time

CAPACITY = "docforge.loadtest.pipelines:build_capacity_pipelines"


def seeded(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 - model timing, not security


def request(schema: type) -> LLMRequest:
    return LLMRequest(system="s", prompt="p", schema=schema, prompt_version="v")


@pytest.mark.parametrize("schema", [RawInvoice, RawPurchaseOrder, RawCoa, RawAnswer])
def test_the_reply_is_valid_for_every_schema_the_service_asks_for(schema: type) -> None:
    reply = minimal_reply(schema)

    schema.model_validate(json.loads(reply))  # type: ignore[attr-defined]


def test_an_answer_says_the_documents_do_not_say() -> None:
    answer = RawAnswer.model_validate_json(minimal_reply(RawAnswer))
    assert answer.statements == [] and answer.unanswerable


def test_the_reply_is_marked_simulated_and_costs_nothing() -> None:
    waits: list[float] = []
    provider = SimulatedProvider("fixed:2.5", sleep=waits.append)

    response = provider.generate(request(RawInvoice))

    assert waits == [2.5]
    assert (response.provider, response.model) == ("simulated", "simulated")
    assert response.input_tokens == 0 and response.output_tokens == 0
    assert response.latency_ms == 2500


def test_model_time_like_c1_has_its_measured_median_and_tail() -> None:
    draw = model_time("c1", seeded(7))
    samples = sorted(draw() for _ in range(4000))

    assert samples[2000] == pytest.approx(14.4, rel=0.08)
    assert samples[3800] == pytest.approx(26.6, rel=0.12)


@pytest.mark.parametrize("spec", ["", "fast", "fixed:", "fixed:-1", "fixed:nan", "fixed:601"])
def test_a_model_time_that_is_not_understood_is_refused(spec: str) -> None:
    with pytest.raises(ValueError, match="SIMULATED_MODEL_TIME"):
        model_time(spec, seeded(0))


def test_zero_model_time_does_not_wait() -> None:
    assert model_time("zero", seeded(0))() == 0


# --- switched on only by name ---------------------------------------------------------------


def settings(**values: str) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_off_by_default() -> None:
    assert settings().simulated_model is False


def test_the_capacity_pipelines_need_the_simulated_model_said_out_loud() -> None:
    with pytest.raises(ValueError, match="SIMULATED_MODEL"):
        settings(PIPELINE_FACTORY=CAPACITY)
    with pytest.raises(ValueError, match="SIMULATED_MODEL"):
        settings(SIMULATED_MODEL="true")  # and the other way round
    assert settings(PIPELINE_FACTORY=CAPACITY, SIMULATED_MODEL="true").simulated_model


def test_in_production_it_needs_no_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "ENVIRONMENT": "production",
        "DATABASE_URL": "postgresql+psycopg://app:s3cr3t@db.internal:5432/docforge",
        "MIGRATION_DATABASE_URL": "postgresql+psycopg://owner:0wn3r@db.internal:5432/docforge",
        "S3_SECRET_KEY": "a-real-secret",
        "WEBHOOK_SIGNING_KEY": "a-real-webhook-key",
        "CONVERTER_URL": "http://converter:8090",
        "CONVERTER_TOKEN": "c" * 32,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    on = settings(PIPELINE_FACTORY=CAPACITY, SIMULATED_MODEL="true")

    assert on.simulated_model


def test_search_and_chat_never_call_a_provider_when_it_is_on() -> None:
    from docforge.search.embeddings import FakeEmbedder
    from docforge.wiring import chat_provider, embedder_for

    on = settings(PIPELINE_FACTORY=CAPACITY, SIMULATED_MODEL="true", GEMINI_API_KEY="k")

    assert isinstance(chat_provider(on), SimulatedProvider)
    assert isinstance(embedder_for(on), FakeEmbedder)


def test_the_capacity_pipelines_use_the_real_parser_and_the_simulated_model() -> None:
    from docforge.extraction.pipeline import InvoicePipeline
    from docforge.loadtest.pipelines import build_capacity_pipelines
    from docforge.parsing.isolation import IsolatedParser

    pipelines = build_capacity_pipelines(
        settings(PIPELINE_FACTORY=CAPACITY, SIMULATED_MODEL="true", SIMULATED_MODEL_TIME="zero")
    )

    assert set(pipelines) == {"invoice", "purchase_order", "coa", "general"}
    invoice = pipelines["invoice"]
    assert isinstance(invoice, InvoicePipeline)
    assert isinstance(invoice.parser, IsolatedParser)
    assert isinstance(invoice.provider, SimulatedProvider)


def test_an_operator_is_told_the_model_is_simulated() -> None:
    from docforge.ops import settings_alerts

    on = settings(PIPELINE_FACTORY=CAPACITY, SIMULATED_MODEL="true")

    (alert,) = settings_alerts(on)
    assert alert.name == "simulated_model" and alert.severity == "warning"
    assert settings_alerts(settings()) == []
