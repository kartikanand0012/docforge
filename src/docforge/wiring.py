"""Building the real service from settings. The API and the worker both start here."""

from importlib import import_module

from docforge.config import Settings
from docforge.db.session import make_engine, make_session_factory
from docforge.documents import DocumentService, Pipeline
from docforge.extraction.pipeline import ExtractionPipeline, InvoicePipeline
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.llm.gemini import GeminiProvider
from docforge.parsing.docling_parser import DoclingParser
from docforge.queue import JobQueue
from docforge.storage import S3ObjectStore


def build_pipeline(settings: Settings) -> InvoicePipeline:
    if settings.gemini_api_key is None:
        raise ValueError("GEMINI_API_KEY is not set")
    provider = GeminiProvider(settings.gemini_model, settings.gemini_api_key.get_secret_value())
    return InvoicePipeline(DoclingParser(), provider, max_pages=settings.max_pages)


def build_pipelines(settings: Settings) -> dict[str, Pipeline]:
    """One pipeline per document type. A new type is added by registering it here."""
    invoice = build_pipeline(settings)
    # The parser and the provider are shared: the parser's models are loaded once.
    order = ExtractionPipeline(
        invoice.parser, invoice.provider, PURCHASE_ORDER_SPEC, max_pages=settings.max_pages
    )
    return {"invoice": invoice, "purchase_order": order}


def load_pipelines(settings: Settings) -> dict[str, Pipeline]:
    """Call the factory named by `PIPELINE_FACTORY` (`module:function`)."""
    module_name, _, function_name = settings.pipeline_factory.partition(":")
    factory = getattr(import_module(module_name), function_name)
    pipelines: dict[str, Pipeline] = factory(settings)
    return pipelines


def build_service(settings: Settings) -> tuple[DocumentService, JobQueue]:
    database_url = settings.database_url.get_secret_value()
    queue = JobQueue(
        database_url,
        max_attempts=settings.job_max_attempts,
        retry_wait_seconds=settings.job_retry_wait_seconds,
    )
    service = DocumentService(
        make_session_factory(make_engine(database_url)),
        S3ObjectStore.from_settings(settings),
        load_pipelines(settings),
        queue.enqueue,
        max_attempts=settings.job_max_attempts,
        max_pending=settings.max_pending_documents,
    )
    queue.bind(service)
    return service, queue
