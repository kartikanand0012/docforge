"""Building the real service from settings. The API and the worker both start here."""

from functools import partial
from importlib import import_module
from importlib.metadata import version
from pathlib import Path

from docforge.anchors import AnchorStore
from docforge.auth import Authenticator
from docforge.chat.service import ChatService
from docforge.config import Settings
from docforge.conversion import Converter, FileConverter, IsolatedConverter, RecordingConverter
from docforge.db.session import make_engine, make_session_factory
from docforge.documents import DocumentService, EventSink, Pipeline
from docforge.extraction.coa import COA_SPEC, CoaExtraction
from docforge.extraction.general import GeneralPipeline
from docforge.extraction.pipeline import INVOICE_SPEC, ExtractionPipeline, InvoicePipeline
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC, PurchaseOrderExtraction
from docforge.llm.base import LLMProvider
from docforge.llm.gemini import GeminiProvider
from docforge.llm.replay import RecordingProvider
from docforge.parsing.cache import CachingParser
from docforge.parsing.docling_parser import DoclingParser
from docforge.parsing.isolation import IsolatedParser
from docforge.queue import JobQueue
from docforge.review.service import ReviewService
from docforge.search.embeddings import Embedder, GeminiEmbedder, RecordingEmbedder
from docforge.search.service import SearchService
from docforge.storage import S3ObjectStore
from docforge.webhooks import WebhookService


def build_pipeline(settings: Settings) -> InvoicePipeline:
    if settings.gemini_api_key is None:
        raise ValueError("GEMINI_API_KEY is not set")
    provider = GeminiProvider(settings.gemini_model, settings.gemini_api_key.get_secret_value())
    parser = IsolatedParser(
        partial(DoclingParser, batch_pages=settings.parser_batch_pages),
        name=DoclingParser.name,
        version=version("docling"),
        max_documents=settings.parser_max_documents,
        timeout_seconds=settings.parser_timeout_seconds,
        max_rss_bytes=settings.parser_max_rss_mb * 1024 * 1024,
        child_env={"HF_HUB_OFFLINE": "1"} if settings.parser_offline else None,
    )
    return InvoicePipeline(parser, provider, max_pages=settings.max_pages)


def build_pipelines(settings: Settings) -> dict[str, Pipeline]:
    """One pipeline per document type. A new type is added by registering it here."""
    invoice = build_pipeline(settings)
    # The parser and the provider are shared: the parser's models are loaded once.
    order = ExtractionPipeline(
        invoice.parser, invoice.provider, PURCHASE_ORDER_SPEC, max_pages=settings.max_pages
    )
    coa: ExtractionPipeline[CoaExtraction] = ExtractionPipeline(
        invoice.parser, invoice.provider, COA_SPEC, max_pages=settings.max_pages
    )
    general = GeneralPipeline(invoice.parser, max_pages=settings.max_pages)
    return {"invoice": invoice, "purchase_order": order, "coa": coa, "general": general}


def build_replay_pipelines(settings: Settings) -> dict[str, Pipeline]:
    """Pipelines that only replay recorded parses and model replies: no key, no network.

    For the end-to-end test and demos on the recorded documents. A file that was never
    recorded fails its parse, which the service records as an unreadable file.
    """
    recordings = Path(settings.recordings_dir)
    parser = CachingParser(recordings / "parsed")
    provider = RecordingProvider(recordings / "llm", settings.gemini_model)
    order: ExtractionPipeline[PurchaseOrderExtraction] = ExtractionPipeline(
        parser, provider, PURCHASE_ORDER_SPEC, max_pages=settings.max_pages
    )
    coa: ExtractionPipeline[CoaExtraction] = ExtractionPipeline(
        parser, provider, COA_SPEC, max_pages=settings.max_pages
    )
    return {
        "invoice": InvoicePipeline(parser, provider, max_pages=settings.max_pages),
        "purchase_order": order,
        "coa": coa,
        "general": GeneralPipeline(parser, max_pages=settings.max_pages),
    }


REPLAY_FACTORY = "docforge.wiring:build_replay_pipelines"


def build_converter(settings: Settings) -> Converter:
    """LibreOffice and Pillow; with replayed pipelines, replayed conversions too, because a
    converted file's recorded parse is keyed by the exact bytes of the PDF made from it."""
    if settings.pipeline_factory == REPLAY_FACTORY:
        return RecordingConverter(Path(settings.recordings_dir) / "renditions", None)
    return IsolatedConverter(
        partial(FileConverter, timeout_seconds=settings.conversion_timeout_seconds),
        timeout_seconds=settings.conversion_timeout_seconds + 30,
        max_rss_bytes=settings.conversion_max_rss_mb * 1024 * 1024,
    )


def load_pipelines(settings: Settings) -> dict[str, Pipeline]:
    """Call the factory named by `PIPELINE_FACTORY` (`module:function`)."""
    module_name, _, function_name = settings.pipeline_factory.partition(":")
    factory = getattr(import_module(module_name), function_name)
    pipelines: dict[str, Pipeline] = factory(settings)
    return pipelines


def build_webhooks(settings: Settings, queue: JobQueue) -> WebhookService:
    database_url = settings.database_url.get_secret_value()
    return WebhookService(
        make_session_factory(make_engine(database_url)),
        settings.webhook_signing_key.get_secret_value().encode(),
        queue.defer_delivery,
        allow_http=settings.webhook_allow_local,
        allow_private=settings.webhook_allow_local,
    )


def build_review(settings: Settings, events: EventSink | None = None) -> ReviewService:
    """The review service, over the same database and store as the document service."""
    database_url = settings.database_url.get_secret_value()
    return ReviewService(
        make_session_factory(make_engine(database_url)),
        S3ObjectStore.from_settings(settings),
        {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC, "coa": COA_SPEC},
        events=events,
    )


def build_authenticator(settings: Settings) -> Authenticator:
    database_url = settings.database_url.get_secret_value()
    return Authenticator(
        make_session_factory(make_engine(database_url)),
        failed_logins_per_window=settings.failed_logins_per_window,
    )


def build_search(settings: Settings) -> SearchService:
    """Search with Gemini embeddings. With no key, or with replayed pipelines, recorded
    embeddings are replayed (tests and demos on the recorded documents), so a replayed run
    ranks exactly as the recorded one; with `CHAT_RECORD=1` and a key, missing ones are
    recorded. Nothing is ever recorded in a deployment."""
    directory = Path(settings.recordings_dir) / "embeddings"
    key = settings.gemini_api_key
    embedder: Embedder
    if settings.pipeline_factory == REPLAY_FACTORY or key is None:
        live = (
            GeminiEmbedder(settings.embedding_model, key.get_secret_value())
            if key is not None and settings.chat_record
            else None
        )
        embedder = RecordingEmbedder(directory, live, model=settings.embedding_model)
    else:
        embedder = GeminiEmbedder(settings.embedding_model, key.get_secret_value())
    sessions = make_session_factory(make_engine(settings.database_url.get_secret_value()))
    return SearchService(sessions, embedder)


def build_chat(settings: Settings, search: SearchService) -> ChatService:
    """Answers with Gemini. With no key, recorded answers are replayed (the demo and the
    browser test); `CHAT_RECORD=1` with a key records what is asked, for those replays."""
    recordings = Path(settings.recordings_dir) / "llm"
    provider: LLMProvider
    if settings.gemini_api_key is None:
        provider = RecordingProvider(recordings, settings.gemini_model)
    else:
        live = GeminiProvider(settings.gemini_model, settings.gemini_api_key.get_secret_value())
        provider = (
            RecordingProvider(recordings, settings.gemini_model, live)
            if settings.chat_record
            else live
        )
    return ChatService(
        make_session_factory(make_engine(settings.database_url.get_secret_value())),
        search,
        provider,
        daily_limit=settings.chat_daily_limit,
    )


def build_service(settings: Settings) -> tuple[DocumentService, JobQueue]:
    database_url = settings.database_url.get_secret_value()
    queue = JobQueue(
        database_url,
        max_attempts=settings.job_max_attempts,
        retry_wait_seconds=settings.job_retry_wait_seconds,
    )
    sessions = make_session_factory(make_engine(database_url))
    webhooks = build_webhooks(settings, queue)
    service = DocumentService(
        sessions,
        S3ObjectStore.from_settings(settings),
        load_pipelines(settings),
        queue.enqueue,
        max_attempts=settings.job_max_attempts,
        max_pending=settings.max_pending_documents,
        events=webhooks,
        anchors=AnchorStore(S3ObjectStore.from_settings(settings)),
        index=queue.defer_index,
        converter=build_converter(settings),
        max_pages=settings.max_pages,
    )
    queue.bind(service)
    queue.bind_search(build_search(settings))
    queue.bind_webhooks(webhooks)
    return service, queue
