"""Exporting traces is switched on by configuration only."""

import pytest
from docforge.telemetry import configure_tracing, document_cost

from docforge.config import Settings


def test_without_an_endpoint_nothing_is_exported() -> None:
    assert configure_tracing(Settings(otel_exporter_otlp_endpoint=None), "api") is None


def test_an_endpoint_gives_an_otlp_exporter_named_for_the_service() -> None:
    provider = configure_tracing(
        Settings(otel_exporter_otlp_endpoint="http://127.0.0.1:4318"), "worker", install=False
    )

    assert provider is not None
    assert provider.resource.attributes["service.name"] == "docforge-worker"
    provider.shutdown()


@pytest.mark.parametrize(
    ("prices", "expected"),
    [(None, None), ((0.30, 2.50), 0.0028)],
)
def test_document_cost_counts_thinking_as_output(
    prices: tuple[float, float] | None, expected: float | None
) -> None:
    # 1,000 in, 800 out, 200 thinking: 1000 x 0.30 + 1000 x 2.50 per million
    assert document_cost([(1000, 800, 200)], prices) == expected
