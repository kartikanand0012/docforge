"""Bulk upload (V2 P6): files dropped together, or one ZIP, each becoming its own document.

Dropped files: `POST /v1/batches` with how many files are coming, then each file to
`POST /v1/batches/{id}/files` (one request each, held to the upload's per-file limit). A ZIP:
`POST /v1/batches/zip`, unpacked on the server. `GET /v1/batches/{id}` counts the batch's
files live; `POST /v1/batches/{id}/retry` reads again those whose reading failed.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from docforge.api.auth import require
from docforge.auth import MEMBER, Principal
from docforge.batches import BatchInvalid, BatchNotFound, BatchService, BatchView, ItemView
from docforge.zipsafe import ZipRefused

Reader = Annotated[Principal, Depends(require("documents:read"))]
Writer = Annotated[Principal, Depends(require("documents:write"))]
BATCH_ZIP_PATH = "/v1/batches/zip"
_NOT_FOUND = HTTPException(404, "No such batch.")


class BatchItemOut(BaseModel):
    id: uuid.UUID
    position: int
    filename: str
    size_bytes: int
    outcome: str  # received, already_received (the same file is in the workspace), refused
    reason: str | None  # why it was refused, in the upload's words
    document_id: uuid.UUID | None
    state: str  # refused, waiting, ready, failed, split, split_review, deleted
    error: str | None  # why its reading failed


class BatchCountsOut(BaseModel):
    total: int  # files that arrived: received + already_received + refused
    not_sent: int  # files the browser said were coming that never arrived
    received: int
    already_received: int
    refused: int
    waiting: int
    processed: int  # every file whose work is over: total - waiting
    ready: int
    split: int  # divided into invoices, or waiting for a person to group its pages
    failed: int  # refused, or its reading failed


class BatchOut(BaseModel):
    id: uuid.UUID
    kind: str  # files, zip
    name: str | None  # the ZIP's
    doc_type: str
    expected: int
    person: str
    created_at: datetime
    counts: BatchCountsOut
    items: list[BatchItemOut]


class BatchSummaryOut(BaseModel):
    id: uuid.UUID
    kind: str
    name: str | None
    person: str
    created_at: datetime
    counts: BatchCountsOut


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: int = Field(ge=1, le=200)
    doc_type: str = "invoice"


def _item(item: ItemView) -> BatchItemOut:
    return BatchItemOut(**vars(item))


def _batch(view: BatchView) -> BatchOut:
    return BatchOut(
        id=view.id, kind=view.kind, name=view.name, doc_type=view.doc_type,
        expected=view.expected, person=view.person, created_at=view.created_at,
        counts=BatchCountsOut(**view.counts), items=[_item(i) for i in view.items],
    )  # fmt: skip


def batches_router(
    batches: BatchService, *, max_upload_bytes: int, max_zip_bytes: int
) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.post("/batches", response_model=BatchOut, status_code=201)
    def create_batch(body: BatchIn, principal: Writer) -> BatchOut:
        """A batch for files about to be sent one by one (up to 50; more is a ZIP)."""
        try:
            view = batches.create(
                principal.tenant_id, expected=body.files, doc_type=body.doc_type,
                actor=principal.actor, person=principal.name,
            )  # fmt: skip
        except BatchInvalid as error:
            raise HTTPException(422, str(error)) from error
        return _batch(view)

    @router.post("/batches/{batch_id}/files", response_model=BatchItemOut, status_code=201)
    async def add_file(batch_id: uuid.UUID, file: UploadFile, principal: Writer) -> BatchItemOut:
        """One file of the batch. A file refused (too large, not accepted, unreadable) is
        recorded with the reason and answered 201 like the rest: the batch goes on."""
        data = await file.read(max_upload_bytes + 1)
        try:
            item = await run_in_threadpool(
                batches.add_file, principal.tenant_id, batch_id, file.filename, data,
                actor=principal.actor, free=principal.role == MEMBER,
            )  # fmt: skip
        except BatchNotFound:
            raise _NOT_FOUND from None
        except BatchInvalid as error:
            raise HTTPException(409, str(error)) from error
        return _item(item)

    @router.post(BATCH_ZIP_PATH.removeprefix("/v1"), response_model=BatchOut, status_code=201)
    async def upload_zip(
        file: UploadFile,
        principal: Writer,
        doc_type: Annotated[str, Form()] = "invoice",
    ) -> BatchOut:
        """Every file of a ZIP (up to 200, 500 MB unpacked) as its own document. A ZIP that
        is unsafe or too large is refused whole, and nothing is made from it."""
        if file.size is not None and file.size > max_zip_bytes:
            raise HTTPException(
                413, f"The ZIP is larger than the limit of {max_zip_bytes // (1024 * 1024)} MB."
            )
        try:
            view = await run_in_threadpool(
                batches.from_zip, principal.tenant_id, file.file, file.filename,
                doc_type=doc_type, actor=principal.actor, person=principal.name,
                free=principal.role == MEMBER,
            )  # fmt: skip
        except ZipRefused as error:
            raise HTTPException(422, error.message) from error
        except BatchInvalid as error:
            raise HTTPException(422, str(error)) from error
        return _batch(view)

    @router.get("/batches", response_model=list[BatchSummaryOut])
    def list_batches(principal: Reader) -> list[BatchSummaryOut]:
        """The newest batches, with their counts."""
        return [
            BatchSummaryOut(
                id=v.id,
                kind=v.kind,
                name=v.name,
                person=v.person,
                created_at=v.created_at,
                counts=BatchCountsOut(**v.counts),
            )
            for v in batches.batches(principal.tenant_id)
        ]

    @router.get("/batches/{batch_id}", response_model=BatchOut)
    def get_batch(batch_id: uuid.UUID, principal: Reader) -> BatchOut:
        try:
            return _batch(batches.batch(principal.tenant_id, batch_id))
        except BatchNotFound:
            raise _NOT_FOUND from None

    @router.post("/batches/{batch_id}/retry", response_model=BatchOut)
    def retry_failed(batch_id: uuid.UUID, principal: Writer) -> BatchOut:
        """Read again the batch's documents whose reading failed; nothing else."""
        try:
            return _batch(batches.retry(principal.tenant_id, batch_id, actor=principal.actor))
        except BatchNotFound:
            raise _NOT_FOUND from None

    return router
