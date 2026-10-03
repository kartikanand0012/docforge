"""Review endpoints and the eval summary: what the review screen and the eval page call.

Tenant is the default one until accounts arrive (C6). The PIN travels in the request body
and is never logged or returned.
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from docforge.api.auth import client_address, require
from docforge.auth import FailureLimiter, Principal, TooManyAttempts
from docforge.documents import DocumentNotFound
from docforge.parsing.base import ParseError
from docforge.review.service import (
    AlreadySigned,
    ApprovalBlocked,
    NotAuthenticated,
    NotPermitted,
    NotReviewable,
    PageNotFound,
    RecordChanged,
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
    # The `record_sha256` of the record the reviewer was shown: a signature binds to it.
    expected_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
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
    record_sha256: str
    meanings: dict[str, str]
    superseded: bool


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
        record_sha256=detail.record_sha256,
        meanings=detail.meanings,
        superseded=detail.superseded,
    )


def _errors(error: Exception) -> HTTPException:
    """The HTTP answer for an expected failure; anything else is re-raised (500, logged)."""
    if isinstance(error, TooManyAttempts):
        return HTTPException(
            429,
            "Too many failed attempts from this address. Try again later.",
            headers={"Retry-After": str(error.retry_after)},
        )
    if isinstance(error, NotPermitted):
        return HTTPException(403, "You can only correct or sign as yourself.")
    if isinstance(error, NotAuthenticated | ReviewerLocked):
        # One answer for unknown email, wrong PIN and a locked account: none says which.
        return _NOT_AUTHENTICATED
    if isinstance(error, AlreadySigned):
        return HTTPException(409, "This version is already signed and can no longer change.")
    if isinstance(error, RecordChanged):
        return HTTPException(
            409, "The record changed after you opened it. Look at it again before signing."
        )
    if isinstance(error, ApprovalBlocked):
        return HTTPException(409, f"{str(error)[0].upper()}{str(error)[1:]}.")
    if isinstance(error, NotReviewable):
        detail = error.args[0] if error.args and isinstance(error.args[0], str) else ""
        return HTTPException(
            409,
            f"{detail[0].upper()}{detail[1:]}." if detail else "There is nothing to review yet.",
        )
    if isinstance(error, DocumentNotFound | PageNotFound):
        return HTTPException(404, "No such document or page.")
    if isinstance(error, ValueError) and not isinstance(error, ValidationError):
        return HTTPException(422, str(error))
    raise error


Reader = Annotated[Principal, Depends(require("documents:read"))]
Reviewing = Annotated[Principal, Depends(require("review"))]


def _person(principal: Principal) -> uuid.UUID:
    """Corrections and signatures are made by a signed-in person, as themselves."""
    if principal.kind != "session":
        raise HTTPException(403, "Only a signed-in reviewer can correct or sign.")
    return principal.subject_id


def review_router(
    review: ReviewService,
    evals_dir: Path | None,
    prices: tuple[float, float] | None,
    limiter: FailureLimiter,
) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/review/queue", response_model=list[QueueItemOut])
    def review_queue(principal: Reviewing) -> list[QueueItemOut]:
        """Documents waiting for a person, oldest first."""
        return [
            QueueItemOut(**{**vars(i), "reasons": list(i.reasons)})
            for i in review.queue(principal.tenant_id)
        ]

    @router.get("/documents/{document_id}/review", response_model=ReviewOut)
    def review_detail(document_id: uuid.UUID, principal: Reviewing) -> ReviewOut:
        try:
            return _review_out(review.detail(principal.tenant_id, document_id))
        except Exception as error:
            raise _errors(error) from error

    @router.post("/documents/{document_id}/corrections", response_model=ReviewOut)
    def correct(
        document_id: uuid.UUID, body: CorrectionIn, principal: Reviewing, request: Request
    ) -> ReviewOut:
        person = _person(principal)
        client = client_address(request)
        try:
            limiter.check(client)
            detail = review.correct(
                principal.tenant_id,
                document_id,
                path=body.path,
                text=body.text,
                reason=body.reason,
                email=body.email,
                pin=body.pin,
                acting_reviewer_id=person,
            )
        except Exception as error:
            if isinstance(error, NotAuthenticated | ReviewerLocked):
                limiter.failed(client)
            raise _errors(error) from error
        return _review_out(detail)

    @router.post("/documents/{document_id}/review", status_code=201, response_model=SignedOut)
    def sign(
        document_id: uuid.UUID, body: SignIn, principal: Reviewing, request: Request
    ) -> SignedOut:
        person = _person(principal)
        client = client_address(request)
        try:
            limiter.check(client)
            signed = review.sign(
                principal.tenant_id,
                document_id,
                outcome=body.outcome,
                meaning=body.meaning,
                reason=body.reason,
                override_reason=body.override_reason,
                expected_record_sha256=body.expected_record_sha256,
                email=body.email,
                pin=body.pin,
                acting_reviewer_id=person,
            )
        except Exception as error:
            if isinstance(error, NotAuthenticated | ReviewerLocked):
                limiter.failed(client)
            raise _errors(error) from error
        return SignedOut(**vars(signed))

    @router.get(
        "/documents/{document_id}/pages/{page}",
        response_class=Response,
        responses={200: {"content": {"image/png": {}}}},
    )
    async def page_image(document_id: uuid.UUID, page: int, principal: Reader) -> Response:
        try:
            png = await run_in_threadpool(review.page_image, principal.tenant_id, document_id, page)
        except ParseError as error:
            raise HTTPException(422, "The page could not be rendered.") from error
        except Exception as error:
            raise _errors(error) from error
        return Response(
            png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
        )

    @router.get("/evals")
    def evals(principal: Reader) -> dict[str, Any]:
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
