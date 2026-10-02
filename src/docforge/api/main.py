"""Wiring for the real service: `uvicorn docforge.api.main:create_default_app --factory`."""

from fastapi import FastAPI

from docforge.api.app import create_app
from docforge.config import Settings, get_settings
from docforge.extraction.pipeline import InvoicePipeline
from docforge.llm.gemini import GeminiProvider
from docforge.parsing.docling_parser import DoclingParser


def build_pipeline(settings: Settings) -> InvoicePipeline:
    if settings.gemini_api_key is None:
        raise ValueError("GEMINI_API_KEY is not set")
    provider = GeminiProvider(settings.gemini_model, settings.gemini_api_key.get_secret_value())
    return InvoicePipeline(DoclingParser(), provider, max_pages=settings.max_pages)


def create_default_app() -> FastAPI:
    settings = get_settings()
    return create_app(build_pipeline(settings), max_upload_bytes=settings.max_upload_bytes)
