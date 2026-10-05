"""Tracing with OpenTelemetry, and the cost of a document's model calls.

Spans carry ids, counts, timings, tokens and outcomes, never document content, questions or
credentials: traces leave for a third-party backend. Exporting is off unless an OTLP endpoint
is configured; without it every span is a no-op.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from docforge.config import Settings

tracer = trace.get_tracer("docforge")


@contextmanager
def traced(name: str) -> Iterator[Span]:
    """A span that records only an error's type, never its message or stack: those can hold
    a search question, a SQL statement's parameters or part of a model reply."""
    with tracer.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as current:
        try:
            yield current
        except BaseException as error:
            current.set_attribute("docforge.error", type(error).__name__)
            current.set_status(Status(StatusCode.ERROR))
            raise


_prices: tuple[float, float] | None = None


def set_prices(prices: tuple[float, float] | None) -> None:
    """USD per million input and output tokens, for the cost on each document's span."""
    global _prices
    _prices = prices


def current_prices() -> tuple[float, float] | None:
    return _prices


def settings_prices(settings: Settings) -> tuple[float, float] | None:
    if (
        settings.price_input_per_million_usd is None
        or settings.price_output_per_million_usd is None
    ):
        return None
    return settings.price_input_per_million_usd, settings.price_output_per_million_usd


def document_cost(
    usages: Iterable[tuple[int, int, int]], prices: tuple[float, float] | None
) -> float | None:
    """USD for (input, output, thinking) token counts; thinking is billed as output.
    None when no prices are configured: a price is never invented."""
    if prices is None:
        return None
    total = sum(i * prices[0] + (o + t) * prices[1] for i, o, t in usages)
    return round(total / 1_000_000, 6)


def configure_tracing(
    settings: Settings, service: str, *, install: bool = True
) -> TracerProvider | None:
    """Export spans over OTLP/HTTP when an endpoint is configured; otherwise do nothing."""
    set_prices(settings_prices(settings))
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": f"docforge-{service}"}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    if install:
        trace.set_tracer_provider(provider)
    return provider
