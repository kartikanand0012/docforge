"""Document endpoints: upload returns at once, a worker does the extraction."""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from docforge.api.uploads import read_pdf_upload, safe_filename
from docforge.db import DEFAULT_TENANT_ID
from docforge.documents import (
    DocumentNotFound,
    DocumentService,
    DocumentTypeConflict,
    QueueFull,
    ReprocessInProgress,
    UnknownDocumentType,
)
from docforge.parsing.base import ParseError
from docforge.parsing.pdf import pdf_page_count
from docforge.storage import StorageUnavailable

logger = logging.getLogger(__name__)

# Until authentication arrives in C6 there is one tenant and one, unnamed, caller.
TENANT = DEFAULT_TENANT_ID
ACTOR = "api:anonymous"


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DocumentOut(_Out):
    id: uuid.UUID
    doc_type: str
    filename: str
    sha256: str
    size_bytes: int
    page_count: int | None
    status: str
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


_NOT_FOUND = HTTPException(404, "No such document.")


def documents_router(
    service: DocumentService, *, max_upload_bytes: int, max_pages: int
) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.post("/documents", status_code=202, response_model=UploadOut)
    async def upload_document(
        response: Response, file: UploadFile, doc_type: Annotated[str, Form()] = "invoice"
    ) -> UploadOut:
        """Store a PDF and queue it for extraction. The same file twice is one document."""
        if doc_type not in service.document_types:
            supported = ", ".join(service.document_types)
            raise HTTPException(422, f"Unknown document type. Supported: {supported}.")
        data = await read_pdf_upload(file, max_upload_bytes)
        try:
            pages = await run_in_threadpool(pdf_page_count, data)
        except ParseError as error:
            raise HTTPException(422, "The file could not be read as a PDF.") from error
        if pages > max_pages:
            raise HTTPException(413, f"The document has {pages} pages; the limit is {max_pages}.")

        try:
            result = await run_in_threadpool(
                service.ingest,
                tenant_id=TENANT,
                doc_type=doc_type,
                filename=safe_filename(file.filename) or "upload.pdf",
                data=data,
                actor=ACTOR,
            )
        except UnknownDocumentType as error:
            raise HTTPException(422, "Unknown document type.") from error
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
    def get_document(document_id: uuid.UUID) -> DocumentDetailOut:
        try:
            detail = service.detail(TENANT, document_id)
        except DocumentNotFound:
            raise _NOT_FOUND from None
        return DocumentDetailOut(
            document=DocumentOut.model_validate(detail.document),
            versions=[VersionOut.model_validate(version) for version in detail.versions],
        )

    @router.get("/documents/{document_id}/extraction", response_model=ExtractionOut)
    def get_extraction(document_id: uuid.UUID) -> ExtractionOut:
        """The extraction of the newest version that succeeded."""
        try:
            latest = service.latest_extraction(TENANT, document_id)
            if latest is None:
                status = service.detail(TENANT, document_id).document.status
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
    def get_assessment(document_id: uuid.UUID) -> AssessmentOut:
        """What was checked on the newest extracted version, and whether a person must look."""
        try:
            detail = service.assessment(TENANT, document_id)
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
    def reprocess_document(document_id: uuid.UUID) -> VersionOut:
        """Queue a new version. Earlier versions and their extractions are kept."""
        try:
            version = service.reprocess(tenant_id=TENANT, document_id=document_id, actor=ACTOR)
        except DocumentNotFound:
            raise _NOT_FOUND from None
        except ReprocessInProgress:
            raise HTTPException(409, "This document is still being processed.") from None
        return VersionOut.model_validate(version)

    @router.get("/documents/{document_id}/audit", response_model=list[AuditEntryOut])
    def get_audit_trail(document_id: uuid.UUID) -> list[AuditEntryOut]:
        try:
            entries = service.audit_trail(TENANT, document_id)
        except DocumentNotFound:
            raise _NOT_FOUND from None
        return [AuditEntryOut.model_validate(entry) for entry in entries]

    @router.get("/audit/verification", response_model=ChainOut)
    def verify_audit_chain() -> ChainOut:
        """Recompute the audit log's hash chain and report the first break, if any.

        `consistent` means the stored hashes agree with the stored entries. It shows that no
        entry was edited or removed from the middle without the later hashes being redone;
        it cannot show that the log was not rewritten by someone able to redo them.
        """
        return ChainOut.model_validate(service.verify_audit_chain(TENANT))

    return router
