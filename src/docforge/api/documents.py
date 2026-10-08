"""Document endpoints: upload returns at once, a worker does the extraction."""

import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.background import BackgroundTask

from docforge.api.auth import require
from docforge.api.uploads import read_upload, safe_filename
from docforge.auth import Principal
from docforge.documents import (
    DocumentNotFound,
    DocumentService,
    DocumentTypeConflict,
    QueueFull,
    ReprocessInProgress,
    UnknownDocumentType,
    UnsupportedFormat,
)
from docforge.formats import ACCEPTED, extension
from docforge.limits import LimitReached, Limits
from docforge.parsing.base import ParseError
from docforge.parsing.pdf import pdf_page_count
from docforge.storage import StorageUnavailable

logger = logging.getLogger(__name__)

Reader = Annotated[Principal, Depends(require("documents:read"))]
Writer = Annotated[Principal, Depends(require("documents:write"))]
_FINAL_STAGES = ("ready", "processed", "failed")
# Each open stream reads the timeline every second; a cap per caller keeps one caller from
# taking every thread and connection.
MAX_STREAMS_PER_CALLER = 5


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DocumentOut(_Out):
    id: uuid.UUID
    doc_type: str
    filename: str
    sha256: str
    media_type: str
    size_bytes: int
    page_count: int | None
    status: str
    stage: str  # stored, parsing, extracting, checking, indexing, processed, ready, ...
    ready_for_chat: bool
    created_at: datetime


class VersionOut(_Out):
    version_no: int
    status: str
    attempts: int
    error: str | None
    parser_version: str | None
    schema_version: str | None
    prompt_version: str | None
    model_id: str | None
    started_at: datetime | None
    finished_at: datetime | None


class UploadOut(BaseModel):
    created: bool  # false when this file was already known
    document: DocumentOut
    version: VersionOut | None


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    next_before: str | None  # pass as `before` for the next page; None at the end


def _cursor(document: DocumentOut) -> str:
    raw = f"{document.created_at.isoformat()}|{document.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _parse_cursor(value: str) -> tuple[datetime, uuid.UUID]:
    """Only what `_cursor` makes: an aware time the database can compare, and an id."""
    try:
        at, _, ident = base64.urlsafe_b64decode(value.encode()).decode().partition("|")
        when = datetime.fromisoformat(at)
        if when.tzinfo is None or not 2000 <= when.astimezone(UTC).year <= 9000:
            raise ValueError("not a time this list gives")
        return when, uuid.UUID(ident)
    except (ValueError, OverflowError) as error:
        raise HTTPException(422, "Not a page cursor from this list.") from error


class StepOut(BaseModel):
    stage: str
    at: datetime
    detail: str | None


class DocumentDetailOut(BaseModel):
    document: DocumentOut
    versions: list[VersionOut]


class ModelRunOut(_Out):
    call_no: int
    provider: str
    model: str
    prompt_version: str
    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None
    latency_ms: float


class ExtractionOut(BaseModel):
    document_id: uuid.UUID
    version: VersionOut
    sha256: str  # of the extraction's canonical JSON, as recorded in the audit log
    extraction: dict[str, Any]  # its shape depends on the document type
    model_runs: list[ModelRunOut]


class MatchOut(BaseModel):
    decision: str  # match or mismatch
    counterpart_document_id: uuid.UUID | None
    discrepancies: list[dict[str, Any]]


class AssessmentOut(BaseModel):
    document_id: uuid.UUID
    version_no: int
    decision: str  # accept or review, taking the match into account
    match_status: str  # match, mismatch or no_counterpart
    assessment: dict[str, Any]  # fields with their page boxes, rule results, unreadable values
    match: MatchOut | None


class AuditEntryOut(_Out):
    id: int
    occurred_at: datetime
    actor: str
    action: str
    details: dict[str, Any]
    prev_hash: str | None
    hash: str


class ChainOut(_Out):
    consistent: bool
    entries: int
    first_bad_id: int | None
    reason: str | None
    anchors_checked: int  # 0 when no anchor has been taken outside the database


_NOT_FOUND = HTTPException(404, "No such document.")


AUDIT_CHECKS_PER_MINUTE = 6


def documents_router(
    service: DocumentService, *, max_upload_bytes: int, max_pages: int, limits: Limits
) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.post("/documents", status_code=202, response_model=UploadOut)
    async def upload_document(
        response: Response,
        file: UploadFile,
        principal: Writer,
        doc_type: Annotated[str, Form()] = "invoice",
    ) -> UploadOut:
        """Store a file and queue it for processing. The same file twice is one document.

        PDFs, office files and images are accepted. A PDF's pages are counted here; any other
        file is made into a PDF in the worker, and its pages are counted there.
        """
        if doc_type not in service.document_types:
            supported = ", ".join(service.document_types)
            raise HTTPException(422, f"Unknown document type. Supported: {supported}.")
        data, fmt = await read_upload(file, max_upload_bytes)
        if fmt == "pdf":
            try:
                pages = await run_in_threadpool(pdf_page_count, data)
            except ParseError as error:
                raise HTTPException(422, "The file could not be read as a PDF.") from error
            if pages > max_pages:
                raise HTTPException(
                    413, f"The document has {pages} pages; the limit is {max_pages}."
                )

        try:
            result = await run_in_threadpool(
                service.ingest,
                tenant_id=principal.tenant_id,
                doc_type=doc_type,
                filename=safe_filename(file.filename) or f"upload.{extension(fmt)}",
                data=data,
                actor=principal.actor,
            )
        except UnknownDocumentType as error:
            raise HTTPException(422, "Unknown document type.") from error
        except UnsupportedFormat as error:
            raise HTTPException(
                415, f"This file type is not accepted. Accepted: {ACCEPTED}."
            ) from error
        except DocumentTypeConflict as error:
            raise HTTPException(409, f"{str(error).capitalize()}.") from error
        except QueueFull as error:
            logger.warning("upload refused: %s", error)
            raise HTTPException(503, "The processing queue is full. Try again later.") from error
        except StorageUnavailable as error:
            logger.warning("upload refused, storage unavailable: %r", error.__cause__)
            raise HTTPException(503, "Storage is unavailable. Try again later.") from error
        response.headers["Location"] = f"/v1/documents/{result.document.id}"
        if not result.created:
            response.status_code = 200
        return UploadOut(
            created=result.created,
            document=DocumentOut.model_validate(result.document),
            version=VersionOut.model_validate(result.version) if result.version else None,
        )

    @router.get("/documents/{document_id}", response_model=DocumentDetailOut)
    def get_document(document_id: uuid.UUID, principal: Reader) -> DocumentDetailOut:
        try:
            detail = service.detail(principal.tenant_id, document_id)
        except DocumentNotFound:
            raise _NOT_FOUND from None
        return DocumentDetailOut(
            document=DocumentOut.model_validate(detail.document),
            versions=[VersionOut.model_validate(version) for version in detail.versions],
        )

    @router.get("/documents/{document_id}/extraction", response_model=ExtractionOut)
    def get_extraction(document_id: uuid.UUID, principal: Reader) -> ExtractionOut:
        """The extraction of the newest version that succeeded."""
        try:
            latest = service.latest_extraction(principal.tenant_id, document_id)
            if latest is None:
                status = service.detail(principal.tenant_id, document_id).document.status
                raise HTTPException(404, f"This document has no extraction yet (status: {status}).")
        except DocumentNotFound:
            raise _NOT_FOUND from None
        return ExtractionOut(
            document_id=document_id,
            version=VersionOut.model_validate(latest.version),
            sha256=latest.extraction.sha256,
            extraction=latest.extraction.data,
            model_runs=[ModelRunOut.model_validate(run) for run in latest.model_runs],
        )

    @router.get("/documents/{document_id}/assessment", response_model=AssessmentOut)
    def get_assessment(document_id: uuid.UUID, principal: Reader) -> AssessmentOut:
        """What was checked on the newest extracted version, and whether a person must look."""
        try:
            detail = service.assessment(principal.tenant_id, document_id)
        except DocumentNotFound:
            raise _NOT_FOUND from None
        if detail is None:
            raise HTTPException(404, "This document has no assessment yet.")
        match = detail.match
        return AssessmentOut(
            document_id=document_id,
            version_no=detail.version.version_no,
            decision=detail.decision,
            match_status=detail.match_status,
            assessment=detail.record.data,
            match=None
            if match is None
            else MatchOut(
                decision=match.decision,
                counterpart_document_id=detail.counterpart_document_id,
                discrepancies=match.data["discrepancies"],
            ),
        )

    @router.post("/documents/{document_id}/reprocess", status_code=202, response_model=VersionOut)
    def reprocess_document(document_id: uuid.UUID, principal: Writer) -> VersionOut:
        """Queue a new version. Earlier versions and their extractions are kept."""
        try:
            version = service.reprocess(
                tenant_id=principal.tenant_id, document_id=document_id, actor=principal.actor
            )
        except DocumentNotFound:
            raise _NOT_FOUND from None
        except ReprocessInProgress:
            raise HTTPException(409, "This document is still being processed.") from None
        return VersionOut.model_validate(version)

    @router.get("/documents", response_model=DocumentPage)
    def list_documents(
        principal: Reader,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        before: str | None = None,
        doc_type: str | None = None,
        stage: str | None = None,
    ) -> DocumentPage:
        """The organisation's documents, newest first, each with its stage."""
        cursor = _parse_cursor(before) if before else None
        rows = service.list_documents(
            principal.tenant_id, limit=limit + 1, before=cursor, doc_type=doc_type, stage=stage
        )
        items = [DocumentOut.model_validate(row) for row in rows[:limit]]
        return DocumentPage(
            items=items, next_before=_cursor(items[-1]) if len(rows) > limit else None
        )

    @router.get("/documents/{document_id}/timeline", response_model=list[StepOut])
    def get_timeline(document_id: uuid.UUID, principal: Reader) -> list[StepOut]:
        """Every stage the document has reached, with when, and the reason if it failed."""
        try:
            steps = service.timeline(principal.tenant_id, document_id)
        except DocumentNotFound:
            raise _NOT_FOUND from None
        return [StepOut(stage=s.stage, at=s.at, detail=s.detail) for s in steps]

    @router.get("/documents/{document_id}/events")
    async def stream_events(document_id: uuid.UUID, principal: Reader) -> StreamingResponse:
        """Server-sent events: each new stage as it is reached, ending once the document is
        ready, processed or failed (or after ten minutes; reconnect to continue)."""
        try:
            await run_in_threadpool(service.timeline, principal.tenant_id, document_id)
        except DocumentNotFound:
            raise _NOT_FOUND from None

        # Held in the database, so the cap holds across every API process; a stream lasts
        # ten minutes at most, so a place left by a process that died frees itself after 11.
        held = limits.hold(
            f"stream:{principal.tenant_id}:{principal.actor}", MAX_STREAMS_PER_CALLER, 660
        )
        try:
            await run_in_threadpool(held.__enter__)
        except LimitReached:
            raise HTTPException(
                429, "Too many open streams; close one or poll the timeline."
            ) from None
        released = False

        async def release() -> None:
            nonlocal released
            if not released:
                released = True
                await run_in_threadpool(held.__exit__, None, None, None)

        async def events() -> AsyncIterator[str]:
            try:
                sent = 0
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    steps = await run_in_threadpool(
                        service.timeline, principal.tenant_id, document_id
                    )
                    for step in steps[sent:]:
                        payload = {"stage": step.stage, "at": step.at.isoformat()}
                        if step.detail:
                            payload["detail"] = step.detail
                        yield f"event: stage\ndata: {json.dumps(payload)}\n\n"
                    sent = len(steps)
                    if steps and steps[-1].stage in _FINAL_STAGES:
                        return
                    await asyncio.sleep(1)
            finally:
                await release()

        # Given back by the stream when it ends, or after the response if it never started.
        return StreamingResponse(
            events(),
            background=BackgroundTask(release),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/documents/{document_id}/audit", response_model=list[AuditEntryOut])
    def get_audit_trail(document_id: uuid.UUID, principal: Reader) -> list[AuditEntryOut]:
        try:
            entries = service.audit_trail(principal.tenant_id, document_id)
        except DocumentNotFound:
            raise _NOT_FOUND from None
        return [AuditEntryOut.model_validate(entry) for entry in entries]

    @router.get("/audit/verification", response_model=ChainOut)
    def verify_audit_chain(principal: Reader) -> ChainOut:
        """Recompute the audit log's hash chain and report the first break, if any.

        `consistent` means the stored hashes agree with the stored entries. It shows that no
        entry was edited or removed from the middle without the later hashes being redone;
        it cannot show that the log was not rewritten by someone able to redo them.
        """
        # Recomputing every hash is linear in the log: a few checks a minute per organisation.
        # Counted per caller: a read-only key cannot use up an administrator's checks.
        key = f"audit-verify:{principal.tenant_id}:{principal.actor}"
        if not limits.allow(key, AUDIT_CHECKS_PER_MINUTE):
            raise HTTPException(429, "The chain was checked a moment ago; wait a minute.")
        return ChainOut.model_validate(service.verify_audit_chain(principal.tenant_id))

    return router
