"""The FastAPI application.

`/v1/documents` is the durable path: upload, queue, worker, stored extraction, audit trail.
`/v1/extractions` is a stateless preview that extracts in the request and stores nothing.
"""

import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError

from docforge import __version__
from docforge.api.agents import agents_router
from docforge.api.auth import require, sessions_router
from docforge.api.chat import chat_router
from docforge.api.collections import collections_router
from docforge.api.documents import documents_router
from docforge.api.exports import exports_router
from docforge.api.review import review_router
from docforge.api.search import search_router
from docforge.api.uploads import (
    DEFAULT_MAX_UPLOAD_BYTES,
    MULTIPART_OVERHEAD,
    read_pdf_upload,
    safe_filename,
    too_large_message,
)
from docforge.api.webhooks import webhooks_router
from docforge.auth import Authenticator, FailureLimiter, Principal
from docforge.chat.service import ChatService
from docforge.collections import CollectionService
from docforge.documents import DocumentService
from docforge.extraction.pipeline import DEFAULT_MAX_PAGES, ExtractionError, InvoicePipeline
from docforge.extraction.schema import InvoiceExtraction
from docforge.limits import Limits, LocalLimits
from docforge.llm.base import LLMError, LLMQuotaExhausted
from docforge.mcp_server.server import mount as mount_mcp
from docforge.mcp_server.tools import AgentTools
from docforge.parsing.base import Block, DocumentTooLarge, NoTextLayer, ParseError
from docforge.review.service import ReviewService
from docforge.search.service import SearchService
from docforge.telemetry import traced
from docforge.webhooks import WebhookService

logger = logging.getLogger(__name__)


class DocumentInfo(BaseModel):
    filename: str | None
    sha256: str
    size_bytes: int
    pages: int


class ParserInfo(BaseModel):
    name: str
    version: str


class ModelRun(BaseModel):
    provider: str
    model: str
    prompt_version: str
    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None
    latency_ms: float


class ExtractionResponse(BaseModel):
    document: DocumentInfo
    parser: ParserInfo
    model_runs: list[ModelRun]  # one per model call; two if the first reply was malformed
    extraction: InvoiceExtraction
    blocks: list[Block] | None  # the parsed blocks that `block_ids` refer to, if asked for


class ErrorResponse(BaseModel):
    detail: str


_ERRORS: dict[int | str, dict[str, object]] = {
    413: {"model": ErrorResponse, "description": "File too large or too many pages"},
    415: {"model": ErrorResponse, "description": "Not a PDF"},
    422: {"model": ErrorResponse, "description": "Unreadable PDF or missing file"},
    502: {"model": ErrorResponse, "description": "The model provider failed"},
    503: {"model": ErrorResponse, "description": "The model quota is used up"},
}


def create_app(
    pipeline: InvoicePipeline | None,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    *,
    service: DocumentService | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    review: ReviewService | None = None,
    evals_dir: Path | None = None,
    prices: tuple[float, float] | None = None,
    cors_origins: Sequence[str] = (),
    authenticator: Authenticator | None = None,
    webhooks: WebhookService | None = None,
    search: SearchService | None = None,
    searches_per_minute: int = 60,
    chat: ChatService | None = None,
    questions_per_minute: int = 20,
    collections: CollectionService | None = None,
    limits: Limits | None = None,
    agents: AgentTools | None = None,
) -> FastAPI:
    """`pipeline` enables the stateless preview endpoint; `service` the document endpoints."""
    # FastAPI's own telemetry is off: its request spans record the query string (a search
    # question) and it would configure exporters from the environment. Requests are traced
    # below, by route only.
    # Kept in the database in a deployment (every API process shares them); here otherwise.
    caps = limits or LocalLimits()
    app = FastAPI(
        title="DocForge",
        version=__version__,
        telemetry={"tracing": False, "metrics": False, "logs": False, "auto_configure": False},
    )
    # Without an authenticator every /v1 route answers 401: there is no anonymous mode.
    app.state.authenticator = authenticator
    if cors_origins:
        # Only the review screen's own origin; credentials are not sent by cookie.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type", "Authorization"],
        )

    @app.middleware("http")
    async def refuse_oversized_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Refuse from the declared length, before the body is spooled to disk. A request
        # without a length is still capped when read; a proxy must cap it in production.
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_upload_bytes + MULTIPART_OVERHEAD:
            return JSONResponse({"detail": too_large_message(max_upload_bytes)}, status_code=413)
        return await call_next(request)

    @app.middleware("http")
    async def trace_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Named by route template, never by path or query: a path can hold an id, a query a
        # question, and neither belongs in a third-party trace store.
        with traced(request.method) as span:
            span.set_attribute("http.request.method", request.method)
            status = 500  # unless a response comes back: an error escaping here becomes a 500
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            finally:
                route = request.scope.get("route")
                template = getattr(route, "path", None) or "unmatched"
                span.update_name(f"{request.method} {template}")
                span.set_attribute("http.route", template)
                span.set_attribute("http.response.status_code", status)

    @app.exception_handler(OperationalError)
    async def database_unavailable(request: Request, error: OperationalError) -> JSONResponse:
        logger.error("database unavailable on %s: %s", request.url.path, type(error.orig).__name__)
        return JSONResponse(
            {"detail": "The database is unavailable. Try again later."}, status_code=503
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "Internal error."}, status_code=500)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if service is not None:
        app.include_router(
            documents_router(
                service, max_upload_bytes=max_upload_bytes, max_pages=max_pages, limits=caps
            )
        )
    limiter = authenticator.limiter if authenticator else FailureLimiter(20, 300)
    if authenticator is not None:
        app.include_router(sessions_router(authenticator))
    if review is not None:
        app.include_router(review_router(review, evals_dir, prices, limiter))
    if webhooks is not None:
        app.include_router(webhooks_router(webhooks))
    if review is not None:
        app.include_router(exports_router(review))
    if search is not None:
        app.include_router(search_router(search, searches_per_minute, caps))
    if chat is not None:
        app.include_router(chat_router(chat, questions_per_minute, caps))
    if collections is not None:
        app.include_router(collections_router(collections))
    if agents is not None and authenticator is not None:
        # AI agents, through the MCP server, with keys administrators make.
        app.include_router(agents_router(authenticator, agents))
        mount_mcp(app, agents, authenticator, list(cors_origins))
    if pipeline is not None:
        _add_preview_endpoint(app, pipeline, max_upload_bytes)
    return app


def _add_preview_endpoint(app: FastAPI, pipeline: InvoicePipeline, max_upload_bytes: int) -> None:
    @app.post("/v1/extractions", response_model=ExtractionResponse, responses=_ERRORS)
    async def create_extraction(
        file: UploadFile,
        principal: Annotated[Principal, Depends(require("documents:write"))],
        include_blocks: bool = False,
    ) -> ExtractionResponse:
        """Extract one born-digital invoice PDF in the request. Nothing is stored."""
        data = await read_pdf_upload(file, max_upload_bytes)

        # Error details stay in the log: provider messages are not for API clients.
        try:
            result = await run_in_threadpool(pipeline.run, data)
        except DocumentTooLarge as error:
            raise HTTPException(413, f"The {error}.") from error
        except NoTextLayer as error:
            raise HTTPException(422, "No text could be read from the PDF.") from error
        except ParseError as error:
            logger.warning("unreadable upload: %s", error)
            raise HTTPException(422, "The file could not be read as a PDF.") from error
        except LLMQuotaExhausted as error:
            logger.warning("model quota exhausted: %s", error)
            raise HTTPException(503, "The model quota is used up. Try again later.") from error
        except LLMError as error:
            logger.warning("model provider failed: %s", error)
            raise HTTPException(502, "The model provider failed. Try again later.") from error
        except ExtractionError as error:
            logger.warning("model reply rejected: %s", error)
            raise HTTPException(502, "The model reply did not fit the schema.") from error

        return ExtractionResponse(
            document=DocumentInfo(
                filename=safe_filename(file.filename),
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                pages=len(result.parsed.pages),
            ),
            parser=ParserInfo(name=result.parsed.parser, version=result.parsed.parser_version),
            model_runs=[
                ModelRun(
                    prompt_version=result.prompt_version, **response.model_dump(exclude={"text"})
                )
                for response in result.responses
            ],
            extraction=result.extraction,
            blocks=list(result.parsed.blocks) if include_blocks else None,
        )
