"""The FastAPI application: one synchronous extraction endpoint."""

import hashlib
import logging

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from docforge import __version__
from docforge.extraction.pipeline import ExtractionError, InvoicePipeline
from docforge.extraction.schema import InvoiceExtraction
from docforge.llm.base import LLMError, LLMQuotaExhausted
from docforge.parsing.base import Block, DocumentTooLarge, ParseError

logger = logging.getLogger(__name__)

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_PDF_MAGIC = b"%PDF-"


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
    pipeline: InvoicePipeline, max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
) -> FastAPI:
    app = FastAPI(title="DocForge", version=__version__)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/extractions", response_model=ExtractionResponse, responses=_ERRORS)
    async def create_extraction(
        file: UploadFile, include_blocks: bool = False
    ) -> ExtractionResponse:
        """Extract one born-digital invoice PDF. Returns when the extraction is done."""
        # The server has already received the body; a reverse proxy must cap request size.
        data = await file.read(max_upload_bytes + 1)
        if len(data) > max_upload_bytes:
            raise HTTPException(
                413, f"The file is larger than the limit of {max_upload_bytes} bytes."
            )
        if not data.startswith(_PDF_MAGIC):
            raise HTTPException(415, "Only PDF files are accepted.")

        # Error details stay in the log: provider messages are not for API clients.
        try:
            result = await run_in_threadpool(pipeline.run, data)
        except DocumentTooLarge as error:
            raise HTTPException(413, f"The {error}.") from error
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
                filename=file.filename,
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

    return app
