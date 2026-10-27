"""Review endpoints and the eval summary: what the review screen and the eval page call.

Tenant is the default one until accounts arrive (C6). The PIN travels in the request body
and is never logged or returned.
"""

import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from docforge.accounts import MAX_PASSWORD
from docforge.api.auth import client_address, require
from docforge.api.einvoice import EinvoiceOut, einvoice_out
from docforge.auth import FailureLimiter, Principal, TooManyAttempts
from docforge.documents import DocumentNotFound
from docforge.parsing.base import ParseError
from docforge.review.flags import MAX_RANGE_DAYS, RangeInvalid
from docforge.review.service import (
    AlreadySigned,
    ApprovalBlocked,
    ClaimedByOther,
    ClaimView,
    NotAuthenticated,
    NotLinkable,
    NotPermitted,
    NotReviewable,
    PageNotFound,
    RecordChanged,
    ReviewDetail,
    ReviewerLocked,
    ReviewService,
    TakeOverRefused,
)

# 403, not 401: the caller's session is fine, only the PIN re-entered for this action failed.
_NOT_AUTHENTICATED = HTTPException(403, "The email or PIN is not right.")
_INDIA = ZoneInfo("Asia/Kolkata")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CorrectionIn(_In):
    path: str = Field(max_length=200)
    text: str | None = Field(max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)
    email: str = Field(max_length=320)
    pin: str = Field(max_length=MAX_PASSWORD)  # an account's password travels here too
    # The `record_sha256` of the record the reviewer was shown, if the caller sends it: a
    # correction made to a record that has changed since is refused.
    expected_record_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ClaimIn(_In):
    # Take the review from whoever has it open now: administrators only.
    take_over: bool = False


class SignIn(_In):
    outcome: Literal["approved", "rejected"]
    meaning: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=2000)
    override_reason: str | None = Field(default=None, max_length=2000)
    # The `record_sha256` of the record the reviewer was shown: a signature binds to it.
    expected_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    email: str = Field(max_length=320)
    pin: str = Field(max_length=MAX_PASSWORD)  # an account's password travels here too


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
    claimed_by: str | None  # another reviewer who has it open now
    flags: "FlagCountsOut"  # what the checks flagged, for badges on the row


class FlagCountsOut(BaseModel):
    """A bill's flags. Duplicates block approval (an override reason is needed); near-expiry
    stock and Schedule H1 lines are for information and never need an override."""

    duplicates: int  # earlier bills this one may repeat
    near_expiry_lines: int
    schedule_h1_lines: int


class NearExpiryLineOut(BaseModel):
    """A line whose stock has less than the workspace's window left after the bill date."""

    path: str  # lines[i]
    product_name: str | None
    batch_no: str | None
    expiry: str  # YYYY-MM
    months_left: int  # from the bill date's month, at least 0 and less than the window


class FlagSummaryOut(BaseModel):
    from_: date = Field(serialization_alias="from")  # India's dates, both included
    to: date
    invoices: int  # invoices received in the range and counted
    duplicates: int  # of those, how many may repeat an earlier bill
    near_expiry_lines: int
    near_expiry_bills: int
    schedule_h1_lines: int
    schedule_h1_bills: int
    # True when the range held more invoices than one summary counts: the counts are then a
    # lower bound.
    capped: bool


class OrderCandidateOut(BaseModel):
    document_id: uuid.UUID
    filename: str
    po_no: str | None
    supplier_name: str | None
    created_at: datetime
    same_supplier: bool  # the invoice's seller, by GSTIN or name


class OrderLinkIn(_In):
    order_document_id: uuid.UUID
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class ClaimOut(BaseModel):
    reviewer_name: str
    claimed_at: datetime
    expires_at: datetime
    mine: bool  # held by the caller


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


class DuplicateOut(BaseModel):
    """An earlier bill this invoice repeats."""

    document_id: uuid.UUID
    filename: str
    matched_on: Literal["irn", "seller_and_number"]


class H1LineOut(BaseModel):
    """A bill line a chemist must enter in the Schedule H1 register."""

    path: str  # lines[i]
    product_name: str
    molecule: str | None  # the schedule's name, when the product names one
    notification: str | None  # the notification that added the molecule
    product_id: uuid.UUID | None  # the workspace's mark, when the product is marked as H1
    joins_on: date | None  # the molecule's day in force, when that is after the bill's date


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
    certificates: list[dict[str, str]]
    claim: ClaimOut | None  # who has it open now, while their claim lasts
    einvoice: EinvoiceOut | None  # an invoice's signed QR, as checked; null if never scanned
    duplicates: list[DuplicateOut]  # earlier bills an invoice repeats; each is also a blocker
    # The workspace's near-expiry window the checks used (line.shelf_life), 0 for off.
    near_expiry_months: int
    schedule_h1: list[H1LineOut]  # an invoice's lines for the H1 register; never a blocker
    # An invoice's lines with less than the window left after the bill date; never a blocker.
    near_expiry: list[NearExpiryLineOut]


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
        certificates=list(detail.certificates),
        claim=_claim_out(detail.claim) if detail.claim else None,
        einvoice=einvoice_out(detail.einvoice),
        duplicates=[
            DuplicateOut(document_id=d.document_id, filename=d.filename, matched_on=d.matched_on)
            for d in detail.duplicates
        ],
        near_expiry_months=detail.near_expiry_months,
        schedule_h1=[
            H1LineOut(
                path=line.path,
                product_name=line.product_name,
                molecule=line.molecule,
                notification=line.notification,
                product_id=line.product_id,
                joins_on=line.joins_on,
            )
            for line in detail.schedule_h1
        ],
        near_expiry=[NearExpiryLineOut(**vars(line)) for line in detail.near_expiry],
    )


def _claim_out(claim: ClaimView) -> ClaimOut:
    return ClaimOut(**vars(claim))


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
        doing = error.args[0] if error.args else "signing"
        return HTTPException(
            409, f"The record changed after you opened it. Look at it again before {doing}."
        )
    if isinstance(error, ClaimedByOther):
        return HTTPException(409, str(error))
    if isinstance(error, TakeOverRefused):
        return HTTPException(403, "Only an administrator can take over a review.")
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


def _india_today() -> date:
    return datetime.now(_INDIA).date()


def _link_errors(error: Exception) -> HTTPException:
    if isinstance(error, NotLinkable):
        return HTTPException(
            409,
            f"This cannot be linked: {error}. Open the invoice, then choose one of the "
            "purchase orders offered.",
        )
    if isinstance(error, ValueError) and not isinstance(error, ValidationError):
        return HTTPException(422, "Say why this is the right order before linking it.")
    return _errors(error)


Reader = Annotated[Principal, Depends(require("documents:read"))]
Reviewing = Annotated[Principal, Depends(require("review"))]


def _person(principal: Principal, doing: str = "correct or sign") -> uuid.UUID:
    """Corrections, signatures and claims are made by a signed-in person, as themselves."""
    if principal.kind != "session":
        raise HTTPException(403, f"Only a signed-in reviewer can {doing}.")
    return principal.subject_id


def _viewer(principal: Principal) -> uuid.UUID | None:
    """The signed-in reviewer asking, if it is one: whose claims are `mine`."""
    return principal.subject_id if principal.kind == "session" else None


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
            QueueItemOut(
                **{**vars(i), "reasons": list(i.reasons), "flags": FlagCountsOut(**vars(i.flags))}
            )
            for i in review.queue(principal.tenant_id, viewer=_viewer(principal))
        ]

    @router.get("/flags/summary", response_model=FlagSummaryOut, response_model_by_alias=True)
    def flag_summary(
        principal: Reader,
        response: Response,
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
    ) -> FlagSummaryOut:
        """What the checks caught on the invoices received from `from` to `to` (India's dates,
        both included; by default the 30 days to today): duplicates, near-expiry lines and
        Schedule H1 lines. Only the caller's workspace is counted."""
        end = to or _india_today()
        start = from_ or end - timedelta(days=29)
        try:
            found = review.flag_summary(principal.tenant_id, start, end)
        except RangeInvalid as error:
            raise HTTPException(
                422,
                f"That date range cannot be counted: {error}. Choose a range of at most "
                f"{MAX_RANGE_DAYS} days that ends on or after its first day.",
            ) from error
        response.headers["Cache-Control"] = "no-store"
        return FlagSummaryOut(**{**vars(found), "from_": found.start, "to": found.end})

    @router.get("/documents/{document_id}/order-candidates", response_model=list[OrderCandidateOut])
    def order_candidates(document_id: uuid.UUID, principal: Reviewing) -> list[OrderCandidateOut]:
        """Purchase orders an invoice can be linked to by hand: the bill's seller's first,
        then the newest."""
        try:
            found = review.order_candidates(principal.tenant_id, document_id)
        except Exception as error:
            raise _link_errors(error) from error
        return [OrderCandidateOut(**vars(candidate)) for candidate in found]

    @router.post("/documents/{document_id}/order-link", response_model=ReviewOut)
    def order_link(document_id: uuid.UUID, body: OrderLinkIn, principal: Reviewing) -> ReviewOut:
        """Link an invoice to its purchase order by hand, with a reason, when nothing on the
        page links them (an order number misprinted, an order placed by phone). The two are
        then compared as usual. Audited on both documents as `match.linked`."""
        _person(principal, "link an order")
        try:
            detail = review.link_order(
                principal.tenant_id, document_id, body.order_document_id,
                reason=body.reason, actor=principal.actor, name=principal.name,
            )  # fmt: skip
        except Exception as error:
            raise _link_errors(error) from error
        return _review_out(detail)

    @router.get("/documents/{document_id}/review", response_model=ReviewOut)
    def review_detail(document_id: uuid.UUID, principal: Reader) -> ReviewOut:
        """The values, their checks and the signature, as a reviewer sees them. Reading it
        needs only read access (the same values are in the extraction and the assessment);
        changing anything needs `review`."""
        try:
            return _review_out(
                review.detail(principal.tenant_id, document_id, viewer=_viewer(principal))
            )
        except Exception as error:
            raise _errors(error) from error

    @router.post("/documents/{document_id}/claim", response_model=ClaimOut)
    def claim(document_id: uuid.UUID, body: ClaimIn, principal: Reviewing) -> ClaimOut:
        """Claim the review for a few minutes, or renew the claim; call again to keep it.

        While another reviewer's claim lasts it is returned unchanged (`mine` false), and
        their changes alone are accepted; an administrator may `take_over`.
        """
        person = _person(principal, "claim a review")
        try:
            claimed = review.claim(
                principal.tenant_id, document_id, reviewer_id=person, take_over=body.take_over
            )
        except Exception as error:
            raise _errors(error) from error
        return _claim_out(claimed)

    @router.delete("/documents/{document_id}/claim", status_code=204)
    def release(document_id: uuid.UUID, principal: Reviewing) -> Response:
        """End the caller's claim. Someone else's is left alone (still 204)."""
        person = _person(principal, "claim a review")
        try:
            review.release(principal.tenant_id, document_id, reviewer_id=person)
        except Exception as error:
            raise _errors(error) from error
        return Response(status_code=204)

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
                expected_record_sha256=body.expected_record_sha256,
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
                (
                    usage["input_tokens"] * prices[0]
                    # Thinking tokens are billed at the output price.
                    + (usage["output_tokens"] + usage.get("thinking_tokens", 0)) * prices[1]
                )
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
