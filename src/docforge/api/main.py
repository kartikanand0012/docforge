"""Entry point for the real service: `uvicorn docforge.api.main:create_default_app --factory`."""

from pathlib import Path

from fastapi import FastAPI

from docforge.api.app import create_app
from docforge.config import get_settings
from docforge.extraction.pipeline import InvoicePipeline
from docforge.wiring import build_authenticator, build_review, build_service, load_pipelines


def create_default_app() -> FastAPI:
    settings = get_settings()
    service, _queue = build_service(settings)
    # The stateless preview endpoint uses the invoice pipeline directly.
    preview = load_pipelines(settings).get("invoice")
    prices = None
    if (
        settings.price_input_per_million_usd is not None
        and settings.price_output_per_million_usd is not None
    ):
        prices = (settings.price_input_per_million_usd, settings.price_output_per_million_usd)
    return create_app(
        preview if isinstance(preview, InvoicePipeline) else None,
        max_upload_bytes=settings.max_upload_bytes,
        service=service,
        max_pages=settings.max_pages,
        review=build_review(settings),
        evals_dir=Path(settings.evals_dir),
        prices=prices,
        cors_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        authenticator=build_authenticator(settings),
    )
