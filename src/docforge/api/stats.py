"""A workspace's figures at a glance, for its home screen: its own documents only."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.documents import DocumentService
from docforge.review.service import ReviewService
from docforge.telemetry import Prices

Reader = Annotated[Principal, Depends(require("documents:read"))]


class StatsOut(BaseModel):
    documents: int
    ready: int  # read and finished
    awaiting_review: int  # in the review queue
    signed: int  # with a signed review
    failed: int
    documents_last_7_days: int
    median_read_seconds: float | None  # null until a document has been read
    model_cost_usd: float | None  # readings and questions; null when a price is unknown


def stats_router(
    service: DocumentService, review: ReviewService | None, prices: Prices | None
) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/stats", response_model=StatsOut)
    async def stats(principal: Reader, response: Response) -> StatsOut:
        """The caller's workspace: how many documents, where they are, and what they cost."""
        found = await run_in_threadpool(service.stats, principal.tenant_id, prices or {})
        waiting = (
            len(await run_in_threadpool(review.queue, principal.tenant_id))
            if review is not None
            else 0
        )
        response.headers["Cache-Control"] = "no-store"
        return StatsOut(awaiting_review=waiting, **vars(found))

    return router
