"""Files holding several invoices (V2 P6): what the splitter proposed, and a person's grouping.

`GET /v1/splits?status=review` lists the files waiting for a person (Split Review);
`GET /v1/documents/{id}/split` shows one, its pages proposed in groups, with the pages in
doubt and why; `POST /v1/documents/{id}/split` confirms a grouping - every page in one
invoice or in "not an invoice page" - and makes the invoices. The page images are
`GET /v1/documents/{id}/pages/{n}?size=thumbnail`.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.documents import (
    DocumentNotFound,
    DocumentService,
    NoRoom,
    SplitDecided,
    SplitGroupView,
    SplitNotFound,
    SplitView,
)
from docforge.split.detect import PagesDoNotReconcile
from docforge.storage import StorageUnavailable

Reader = Annotated[Principal, Depends(require("documents:read"))]
Reviewer = Annotated[Principal, Depends(require("review"))]


class SplitGroupOut(BaseModel):
    pages: list[int]
    invoice_no: str | None  # as read from its first page, when proposed
    seller_gstin: str | None
    document_id: uuid.UUID | None  # the invoice made from these pages
    document_status: str | None
    new: bool  # false: these pages were a document already


class SplitCountsOut(BaseModel):
    """parent pages = invoice pages + non-invoice pages, always."""

    pages: int
    invoices: int
    invoice_pages: int
    non_invoice_pages: int


class SplitOut(BaseModel):
    document_id: uuid.UUID
    filename: str
    status: str  # review: waiting for a person; split: the invoices are made
    automatic: bool  # made by the splitter, every page certain
    reason: str | None  # why a person must confirm
    page_count: int
    proposed: list[SplitGroupOut]
    proposed_non_invoice: list[int]
    pages_in_doubt: list[int]
    groups: list[SplitGroupOut]  # as made; empty while waiting
    non_invoice: list[int]
    counts: SplitCountsOut  # of what was made, or while waiting of the proposal
    decided_by: str | None
    decided_person: str | None
    decided_at: datetime | None
    created_at: datetime


class SplitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: list[list[int]] = Field(max_length=500)  # one list of page numbers per invoice
    non_invoice: list[int] = Field(default_factory=list, max_length=1000)


def _group(group: SplitGroupView) -> SplitGroupOut:
    return SplitGroupOut(
        pages=list(group.pages), invoice_no=group.invoice_no, seller_gstin=group.seller_gstin,
        document_id=group.document_id, document_status=group.document_status, new=group.new,
    )  # fmt: skip


def split_out(view: SplitView) -> SplitOut:
    made = view.status == "split"
    groups = view.groups if made else view.proposed
    outside = view.non_invoice if made else view.proposed_non_invoice
    counts = SplitCountsOut(
        pages=view.page_count,
        invoices=len(groups),
        invoice_pages=sum(len(group.pages) for group in groups),
        non_invoice_pages=len(outside),
    )
    return SplitOut(
        document_id=view.document_id, filename=view.filename, status=view.status,
        automatic=view.automatic, reason=view.reason, page_count=view.page_count,
        proposed=[_group(g) for g in view.proposed],
        proposed_non_invoice=list(view.proposed_non_invoice),
        pages_in_doubt=list(view.pages_in_doubt),
        groups=[_group(g) for g in view.groups], non_invoice=list(view.non_invoice),
        counts=counts, decided_by=view.decided_by, decided_person=view.decided_person,
        decided_at=view.decided_at, created_at=view.created_at,
    )  # fmt: skip


def splits_router(service: DocumentService) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/splits", response_model=list[SplitOut])
    def list_splits(
        principal: Reader,
        status: Literal["review", "split", "all"] = "review",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[SplitOut]:
        """Files holding several invoices, newest first: by default those waiting for a person
        to group their pages."""
        views = service.splits(
            principal.tenant_id, status=None if status == "all" else status, limit=limit
        )
        return [split_out(view) for view in views]

    @router.get("/documents/{document_id}/split", response_model=SplitOut)
    def get_split(document_id: uuid.UUID, principal: Reader) -> SplitOut:
        try:
            return split_out(service.split_view(principal.tenant_id, document_id))
        except (DocumentNotFound, SplitNotFound):
            raise HTTPException(404, "This document was not divided into invoices.") from None

    @router.post("/documents/{document_id}/split", response_model=SplitOut)
    async def confirm_split(document_id: uuid.UUID, body: SplitIn, principal: Reviewer) -> SplitOut:
        """Confirm how the file's pages group into invoices: each group becomes an invoice,
        read as any upload is. Every page must be in one group or in `non_invoice`."""
        try:
            view = await run_in_threadpool(
                service.confirm_split, principal.tenant_id, document_id, body.groups,
                body.non_invoice, actor=principal.actor, person=principal.name,
            )  # fmt: skip
        except (DocumentNotFound, SplitNotFound):
            raise HTTPException(404, "This document was not divided into invoices.") from None
        except SplitDecided as error:
            raise HTTPException(409, str(error)) from None
        except PagesDoNotReconcile as error:
            raise HTTPException(422, str(error)) from None
        except NoRoom as error:
            raise HTTPException(429, str(error)) from None
        except StorageUnavailable as error:
            raise HTTPException(
                503, "Storage is unavailable. Try again later.", headers={"Retry-After": "30"}
            ) from error
        return split_out(view)

    return router
