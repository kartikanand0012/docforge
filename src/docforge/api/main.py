"""Entry point for the real service: `uvicorn docforge.api.main:create_default_app --factory`."""

from pathlib import Path

from fastapi import FastAPI

from docforge.api.app import create_app
from docforge.collections import CollectionService
from docforge.config import get_settings
from docforge.db.session import make_engine, make_session_factory
from docforge.extraction.pipeline import InvoicePipeline
from docforge.telemetry import configure_tracing, settings_prices
from docforge.wiring import (
    build_authenticator,
    build_chat,
    build_review,
    build_search,
    build_service,
    build_webhooks,
    load_pipelines,
)


def create_default_app() -> FastAPI:
    settings = get_settings()
    configure_tracing(settings, "api")
    service, queue = build_service(settings)
    webhooks = build_webhooks(settings, queue)
    # The stateless preview endpoint uses the invoice pipeline directly.
    preview = load_pipelines(settings).get("invoice")
    prices = settings_prices(settings)
    search = build_search(settings)
    return create_app(
        preview if isinstance(preview, InvoicePipeline) else None,
        max_upload_bytes=settings.max_upload_bytes,
        service=service,
        max_pages=settings.max_pages,
        review=build_review(settings, events=webhooks),
        evals_dir=Path(settings.evals_dir),
        prices=prices,
        cors_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        authenticator=build_authenticator(settings),
        webhooks=webhooks,
        search=search,
        chat=build_chat(settings, search),
        collections=CollectionService(
            make_session_factory(make_engine(settings.database_url.get_secret_value()))
        ),
    )
