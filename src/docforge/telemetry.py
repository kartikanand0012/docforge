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


Prices = dict[str, tuple[float, float]]  # "provider/model" -> USD per million in, out
_prices: Prices = {}


def set_prices(prices: Prices) -> None:
    """USD per million input and output tokens by "provider/model", for each document's cost."""
    global _prices
    _prices = dict(prices)


def current_prices() -> Prices:
    return _prices


def settings_prices(settings: Settings) -> tuple[float, float] | None:
    """Gemini's price, for the eval page: its reports are Gemini's."""
    return settings.prices().get(f"gemini/{settings.gemini_model}")


def document_cost(usages: Iterable[tuple[str, str, int, int, int]], prices: Prices) -> float | None:
    """USD for each call's (provider, model, input, output, thinking); thinking is billed as
    output. None when any call's model has no price: a price is never invented, and a
    document is never half priced."""
    if not prices:
        return None
    total = 0.0
    for provider, model, i, o, t in usages:
        price = prices.get(f"{provider}/{model}")
        if price is None:
            return None
        total += i * price[0] + (o + t) * price[1]
    return round(total / 1_000_000, 6)


def configure_tracing(
    settings: Settings, service: str, *, install: bool = True
) -> TracerProvider | None:
    """Export spans over OTLP/HTTP when an endpoint is configured; otherwise do nothing."""
    set_prices(settings.prices())
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
