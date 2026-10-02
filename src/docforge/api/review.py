"""Review endpoints and the eval summary: what the review screen and the eval page call.

Tenant is the default one until accounts arrive (C6). The PIN travels in the request body
and is never logged or returned.
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from docforge.db import DEFAULT_TENANT_ID
from docforge.parsing.base import ParseError
from docforge.review.service import (
    AlreadySigned,
    ApprovalBlocked,
    NotAuthenticated,
    NotReviewable,
    ReviewDetail,
    ReviewerLocked,
    ReviewService,
)

_NOT_AUTHENTICATED = HTTPException(401, "The email or PIN is not right.")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CorrectionIn(_In):
    path: str = Field(max_length=200)
    text: str | None = Field(max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)
    email: str = Field(max_length=320)
    pin: str = Field(max_length=64)


class SignIn(_In):
    outcome: Literal["approved", "rejected"]
    meaning: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=2000)
    override_reason: str | None = Field(default=None, max_length=2000)
    email: str = Field(max_length=320)
    pin: str = Field(max_length=64)


class PageOut(BaseModel):
    number: int
    width: float  # points
    height: float


class QueueItemOut(BaseModel):
    document_id: uuid.UUID
    doc_type: str
    filename: str
    version_no: int
    created_at: datetime
    reasons: list[str]
    match_status: str


class CorrectionOut(BaseModel):
    path: str
    old_text: str | None
    new_text: str | None
    reason: str
    reviewer_name: str
    created_at: datetime


class SignedOut(BaseModel):
    outcome: str
    meaning: str
    reason: str
    override_reason: str | None
    reviewer_name: str
    signed_at: datetime
    record_sha256: str
    draft: dict[str, Any] | None


class ReviewOut(BaseModel):
    document_id: uuid.UUID
    doc_type: str
    filename: str
    version_no: int
    page_count: int
    pages: list[PageOut]
    decision: str
    blockers: list[str]
    record: dict[str, Any]
    assessment: dict[str, Any]
    editable_paths: list[str]
    corrections: list[CorrectionOut]
    match_status: str
    discrepancies: list[dict[str, Any]]
    counterpart_document_id: uuid.UUID | None
    review: SignedOut | None
    signature_valid: bool | None


def _review_out(detail: ReviewDetail) -> ReviewOut:
    return ReviewOut(
        document_id=detail.document_id,
        doc_type=detail.doc_type,
        filename=detail.filename,
        version_no=detail.version_no,
        page_count=detail.page_count,
        pages=[PageOut(**page) for page in detail.pages],
        decision=detail.decision,
        blockers=list(detail.blockers),
        record=detail.record,
        assessment=detail.assessment.model_dump(mode="json"),
        editable_paths=list(detail.editable_paths),
        corrections=[CorrectionOut(**vars(c)) for c in detail.corrections],
        match_status=detail.match_status,
        discrepancies=[d.model_dump(mode="json") for d in detail.discrepancies],
        counterpart_document_id=detail.counterpart_document_id,
        review=SignedOut(**vars(detail.review)) if detail.review else None,
        signature_valid=detail.signature_valid,
    )


def _errors(error: Exception) -> HTTPException:
    if isinstance(error, NotAuthenticated):
        return _NOT_AUTHENTICATED
    if isinstance(error, ReviewerLocked):
        return HTTPException(423, "Too many wrong PINs. Try again in 15 minutes.")
    if isinstance(error, AlreadySigned):
        return HTTPException(409, "This version is already signed and can no longer change.")
    if isinstance(error, ApprovalBlocked):
        return HTTPException(409, f"{str(error)[0].upper()}{str(error)[1:]}.")
    if isinstance(error, NotReviewable):
        return HTTPException(409, "There is no extraction of this document to review yet.")
    if isinstance(error, LookupError):
        return HTTPException(404, "No such document or page.")
    if isinstance(error, ValueError):
        return HTTPException(422, str(error))
    raise error


def review_router(
    review: ReviewService, evals_dir: Path | None, prices: tuple[float, float] | None
) -> APIRouter:
    router = APIRouter(prefix="/v1")
    tenant = DEFAULT_TENANT_ID

    @router.get("/review/queue", response_model=list[QueueItemOut])
    def review_queue() -> list[QueueItemOut]:
        """Documents waiting for a person, oldest first."""
        return [
            QueueItemOut(**{**vars(i), "reasons": list(i.reasons)}) for i in review.queue(tenant)
        ]

    @router.get("/documents/{document_id}/review", response_model=ReviewOut)
    def review_detail(document_id: uuid.UUID) -> ReviewOut:
        try:
            return _review_out(review.detail(tenant, document_id))
        except Exception as error:
            raise _errors(error) from error

    @router.post("/documents/{document_id}/corrections", response_model=ReviewOut)
    def correct(document_id: uuid.UUID, body: CorrectionIn) -> ReviewOut:
        try:
            detail = review.correct(
                tenant,
                document_id,
                path=body.path,
                text=body.text,
                reason=body.reason,
                email=body.email,
                pin=body.pin,
            )
        except Exception as error:
            raise _errors(error) from error
        return _review_out(detail)

    @router.post("/documents/{document_id}/review", status_code=201, response_model=SignedOut)
    def sign(document_id: uuid.UUID, body: SignIn) -> SignedOut:
        try:
            signed = review.sign(
                tenant,
                document_id,
                outcome=body.outcome,
                meaning=body.meaning,
                reason=body.reason,
                override_reason=body.override_reason,
                email=body.email,
                pin=body.pin,
            )
        except Exception as error:
            raise _errors(error) from error
        return SignedOut(**vars(signed))

    @router.get(
        "/documents/{document_id}/pages/{page}",
        response_class=Response,
        responses={200: {"content": {"image/png": {}}}},
    )
    async def page_image(document_id: uuid.UUID, page: int) -> Response:
        try:
            png = await run_in_threadpool(review.page_image, tenant, document_id, page)
        except ParseError as error:
            raise HTTPException(422, "The page could not be rendered.") from error
        except Exception as error:
            raise _errors(error) from error
        return Response(
            png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
        )

    @router.get("/evals")
    def evals() -> dict[str, Any]:
        """The committed eval reports, summarised for the eval and cost page."""
        if evals_dir is None:
            raise HTTPException(404, "No eval reports are configured.")
        return summarise_evals(evals_dir, prices)

    return router


def _load(directory: Path, name: str) -> dict[str, Any] | None:
    path = directory / f"{name}.json"
    if not path.is_file():
        return None
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def summarise_evals(directory: Path, prices: tuple[float, float] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    invoice = _load(directory, "invoice")
    if invoice is not None:
        summary, usage = invoice["summary"], invoice["usage"]
        documents = max(summary["documents"], 1)
        out["extraction"] = {
            "model": invoice["model"],
            "documents": summary["documents"],
            "documents_fully_correct": summary["documents_fully_correct"],
            "fields_correct": summary["fields"]["correct"],
            "fields_total": summary["fields"]["total"],
            "citation_accuracy": summary["citations"]["accuracy"],
            "latency_ms_p50": usage["latency_ms_p50"],
            "latency_ms_p95": usage["latency_ms_p95"],
            "input_tokens_per_document": usage["input_tokens"] // documents,
            "output_tokens_per_document": usage["output_tokens"] // documents,
        }
        per_document = None
        if prices is not None:
            per_document = round(
                (usage["input_tokens"] * prices[0] + usage["output_tokens"] * prices[1])
                / 1_000_000
                / documents,
                6,
            )
        out["cost"] = {
            "price_input_per_million_usd": prices[0] if prices else None,
            "price_output_per_million_usd": prices[1] if prices else None,
            "per_document_usd": per_document,
        }
    trust = _load(directory, "trust")
    if trust is not None:
        out["trust"] = dict(trust["summary"])
    scans = _load(directory, "scans")
    if scans is not None:
        out["scans"] = {
            name: {
                "source": variant["source"],
                "fields_correct": variant["summary"]["fields"]["correct"],
                "fields_total": variant["summary"]["fields"]["total"],
                "documents_with_errors": variant["documents_with_errors"],
                "silent_errors": variant["silent_errors"],
                "silent_errors_after_order_match": variant["silent_errors_after_order_match"],
            }
            for name, variant in scans["variants"].items()
        }
    multipage = _load(directory, "multipage")
    if multipage is not None:
        out["multipage"] = {
            "documents": multipage["summary"]["documents"],
            "documents_fully_correct": multipage["summary"]["documents_fully_correct"],
            "fields_correct": multipage["summary"]["fields"]["correct"],
            "fields_total": multipage["summary"]["fields"]["total"],
            "pages": multipage["usage"]["pages"],
        }
    return out
