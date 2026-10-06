"""Exporting traces is switched on by configuration only."""

from docforge.config import Settings
from docforge.telemetry import configure_tracing, document_cost


def test_without_an_endpoint_nothing_is_exported() -> None:
    assert configure_tracing(Settings(otel_exporter_otlp_endpoint=None), "api") is None


def test_an_endpoint_gives_an_otlp_exporter_named_for_the_service() -> None:
    provider = configure_tracing(
        Settings(otel_exporter_otlp_endpoint="http://127.0.0.1:4318"), "worker", install=False
    )

    assert provider is not None
    assert provider.resource.attributes["service.name"] == "docforge-worker"
    provider.shutdown()


PRICES = {"gemini/flash": (0.30, 2.50), "anthropic/sonnet": (3.0, 15.0)}


def test_document_cost_counts_thinking_as_output() -> None:
    # 1,000 in, 800 out, 200 thinking: 1000 x 0.30 + 1000 x 2.50 per million
    assert document_cost([("gemini", "flash", 1000, 800, 200)], PRICES) == 0.0028


def test_each_call_is_priced_by_its_own_provider_and_model() -> None:
    calls = [("gemini", "flash", 1000, 800, 200), ("anthropic", "sonnet", 1000, 1000, 0)]
    assert document_cost(calls, PRICES) == round(0.0028 + 0.018, 6)


def test_no_cost_is_shown_if_any_call_has_no_price() -> None:
    assert document_cost([("gemini", "flash", 1, 1, 0), ("openai", "o", 1, 1, 0)], PRICES) is None
    assert document_cost([("gemini", "flash", 1, 1, 0)], {}) is None


def test_no_prices_means_no_cost_even_without_calls() -> None:
    assert document_cost([], {}) is None
