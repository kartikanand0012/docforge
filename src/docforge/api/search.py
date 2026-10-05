"""Search over the organisation's documents, with citations."""

import threading
import time
import uuid
from collections import deque
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from docforge.api.auth import require
from docforge.auth import Principal
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
    boxes: list[dict[str, Any]]


class SearchOut(BaseModel):
    query: str
    mode: str
    # True when a hybrid search could not ask the embedding service (no key, or its quota used
    # up) and answered from words alone.
    words_only: bool = False
    results: list[HitOut]


class _PerCaller:
    """Searches per caller in the last minute, in this process: each vector search is a paid
    call, so one credential cannot use up the organisation's quota."""

    def __init__(self, per_minute: int) -> None:
        self._limit = per_minute
        self._seen: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, caller: str) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = self._seen.setdefault(caller, deque())
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if len(recent) >= self._limit:
                return False
            recent.append(now)
            return True


def search_router(search: SearchService, per_minute: int = 60) -> APIRouter:
    router = APIRouter(prefix="/v1")
    limiter = _PerCaller(per_minute)

    @router.get("/search", response_model=SearchOut)
    async def run_search(
        principal: Reader,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        k: Annotated[int, Query(ge=1, le=50)] = 10,
        mode: Literal["keyword", "vector", "hybrid"] = "hybrid",
        doc_type: str | None = None,
    ) -> SearchOut:
        if not limiter.allow(principal.actor):
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
            results=[HitOut(**{**vars(hit), "boxes": list(hit.boxes)}) for hit in hits],
        )

    return router
