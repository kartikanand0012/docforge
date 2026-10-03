"""Search over the organisation's documents, with citations."""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from docforge.api.auth import require
from docforge.auth import Principal
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
    results: list[HitOut]


def search_router(search: SearchService) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/search", response_model=SearchOut)
    async def run_search(
        principal: Reader,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        k: Annotated[int, Query(ge=1, le=50)] = 10,
        mode: Literal["keyword", "vector", "hybrid"] = "hybrid",
        doc_type: str | None = None,
    ) -> SearchOut:
        hits = await run_in_threadpool(
            lambda: search.search(principal.tenant_id, q, k=k, mode=mode, doc_type=doc_type)
        )
        return SearchOut(
            query=q,
            mode=mode,
            results=[HitOut(**{**vars(hit), "boxes": list(hit.boxes)}) for hit in hits],
        )

    return router
