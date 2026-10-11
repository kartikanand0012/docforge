"""Search over the organisation's documents, with citations."""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.limits import Limits
from docforge.search.embeddings import EmbeddingMissing, EmbeddingUnavailable
from docforge.search.service import SearchService

Reader = Annotated[Principal, Depends(require("documents:read"))]


class HitOut(BaseModel):
    document_id: uuid.UUID
    doc_type: str
    filename: str
    kind: str
    page: int
    text: str
    score: float
    # True: the passage holds at least one of the question's words. False: it is only close
    # in meaning. When no result holds them, nothing printed the words asked for.
    matched_words: bool
    boxes: list[dict[str, Any]]


class SearchOut(BaseModel):
    query: str
    mode: str
    # True when a hybrid search could not ask the embedding service (no key, or its quota used
    # up) and answered from words alone.
    words_only: bool = False
    results: list[HitOut]


def search_router(search: SearchService, per_minute: int, limits: Limits) -> APIRouter:
    """Searches are counted per caller and minute, across every API process: each vector
    search is a paid call, so one credential cannot use up the organisation's quota."""
    router = APIRouter(prefix="/v1")

    @router.get("/search", response_model=SearchOut)
    async def run_search(
        principal: Reader,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        k: Annotated[int, Query(ge=1, le=50)] = 10,
        mode: Literal["keyword", "vector", "hybrid"] = "hybrid",
        doc_type: str | None = None,
    ) -> SearchOut:
        key = f"search:{principal.tenant_id}:{principal.actor}"
        if not await run_in_threadpool(limits.allow, key, per_minute):
            raise HTTPException(
                429, "Too many searches. Try again in a minute.", headers={"Retry-After": "60"}
            )
        try:
            hits = await run_in_threadpool(
                lambda: search.search(principal.tenant_id, q, k=k, mode=mode, doc_type=doc_type)
            )
        except (EmbeddingMissing, EmbeddingUnavailable) as error:
            raise HTTPException(
                503, "Search by meaning is unavailable just now. Try words only."
            ) from error
        return SearchOut(
            query=q,
            mode=mode,
            words_only=getattr(hits, "words_only", False),
            # Block texts stay server-side: chat uses them to place quotes; search shows boxes.
            results=[
                HitOut(**{**vars(hit), "boxes": list(hit.boxes), "blocks": None}) for hit in hits
            ],
        )

    return router
