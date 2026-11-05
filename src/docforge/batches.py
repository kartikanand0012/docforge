"""Many files at once (V2 P6): up to 50 dropped together, or one ZIP of up to 200.

Each file becomes its own document exactly as an upload does: the upload's checks
(`upload_checks.check_file`, with its words), then the upload's entry point
(`DocumentService.ingest`), so the same bytes already in the workspace are reported "already
received" and not read again. One bad file never fails the batch: it is recorded with its
reason and the rest go on.

Files dropped together are sent by the browser one request each into a batch made first
(`create`, then `add_file`): each is held to the per-file limit as an upload is, and the
batch keeps what arrived. A ZIP is sent whole and unpacked here (`zipsafe`), every check made
before the first document is created. Either way the reading is the worker's, as for any
upload: leaving the page stops nothing that has arrived.

A batch's view counts its files live from their documents: waiting, ready, failed, split into
invoices, and the files refused when they arrived. A retry reads again only the documents
whose reading failed.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docforge import audit
from docforge.accounts import MemberCaps
from docforge.api.uploads import safe_filename
from docforge.db.models import Document, DocumentVersion, UploadBatch, UploadBatchItem, live
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.documents import (
    DailyReadingsUsed,
    DocumentService,
    DocumentTypeConflict,
    FreeReadingsUsed,
    QueueFull,
    ReprocessInProgress,
    SplitFile,
    WorkspaceFull,
)
from docforge.storage import StorageUnavailable
from docforge.upload_checks import FileRefused, check_file
from docforge.zipsafe import MAX_DOCUMENTS, open_zip

MAX_FILES = 50  # dropped together; more is a ZIP
Outcome = Literal["received", "already_received", "refused"]

QUEUE_FULL = "The processing queue is full. Upload this file again later."
STORAGE_DOWN = "Storage was unavailable. Upload this file again."
WORKSPACE_FULL = "This free workspace is full: delete a document, then upload this file again."
READINGS_USED = "Today's readings are used up: upload this file again tomorrow."
NOT_ARRIVED = "The file did not finish arriving. Upload it again."


class BatchNotFound(LookupError):
    pass


class BatchInvalid(ValueError):
    """Refused in words: a batch too large, or full."""


@dataclass(frozen=True)
class ItemView:
    id: uuid.UUID
    position: int
    filename: str
    size_bytes: int
    outcome: str  # received, already_received, refused
    reason: str | None  # why it was refused, in the upload's words
    document_id: uuid.UUID | None
    state: str  # refused, waiting, ready, failed, split, split_review, deleted
    error: str | None  # why its reading failed


@dataclass(frozen=True)
class BatchView:
    id: uuid.UUID
    kind: str  # files, zip
    name: str | None
    doc_type: str
    expected: int
    created_by: str
    person: str
    created_at: datetime
    items: tuple[ItemView, ...]

    def count(self, *states: str) -> int:
        return sum(1 for item in self.items if item.state in states)

    @property
    def counts(self) -> dict[str, int]:
        """total = received + already received + refused; every file in exactly one state."""
        waiting = self.count("waiting")
        return {
            "total": len(self.items),
            "not_sent": max(self.expected - len(self.items), 0),
            "received": sum(1 for i in self.items if i.outcome == "received"),
            "already_received": sum(1 for i in self.items if i.outcome == "already_received"),
            "refused": self.count("refused"),
            "waiting": waiting,
            "processed": len(self.items) - waiting,
            "ready": self.count("ready"),
            "split": self.count("split", "split_review"),
            "failed": self.count("failed", "refused"),
        }


def _state(outcome: str, status: str | None) -> str:
    if outcome == "refused":
        return "refused"
    if status is None:
        return "deleted"
    return {
        "extracted": "ready",
        "failed": "failed",
        "split": "split",
        "split_review": "split_review",
    }.get(status, "waiting")


class BatchService:
    def __init__(
        self,
        sessions: SessionFactory,
        documents: DocumentService,
        *,
        max_upload_bytes: int = 10 * 1024 * 1024,
        max_pages: int = 20,
        member_caps: MemberCaps | None = None,
    ) -> None:
        self._sessions = sessions
        self._documents = documents
        self._max_upload_bytes = max_upload_bytes
        self._max_pages = max_pages
        self._caps = member_caps or MemberCaps()

    # --- making a batch ----------------------------------------------------------------

    @scoped
    def create(
        self,
        tenant_id: uuid.UUID,
        *,
        expected: int,
        doc_type: str,
        actor: str,
        person: str,
    ) -> BatchView:
        """A batch for `expected` files the browser is about to send, one request each."""
        if not 1 <= expected <= MAX_FILES:
            raise BatchInvalid(
                f"Up to {MAX_FILES} files can be uploaded at once. For more, put them in a ZIP "
                f"(up to {MAX_DOCUMENTS} files)."
            )
        self._known_type(doc_type)
        with self._sessions.begin() as session:
            batch = UploadBatch(
                tenant_id=tenant_id, kind="files", doc_type=doc_type, expected=expected,
                created_by=actor, person=person,
            )  # fmt: skip
            session.add(batch)
            session.flush()
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="batch.created",
                target_type="upload_batch", target_id=str(batch.id),
                details={"kind": "files", "files": expected},
            )  # fmt: skip
            batch_id = batch.id
        return self.batch(tenant_id, batch_id)

    @scoped
    def add_file(
        self,
        tenant_id: uuid.UUID,
        batch_id: uuid.UUID,
        filename: str | None,
        data: bytes,
        *,
        actor: str,
        free: bool,
    ) -> ItemView:
        """One file of a batch, taken as an upload is. Raises `BatchNotFound`, or
        `BatchInvalid` when the batch has all its files."""
        with self._sessions.begin() as session:
            batch = self._row(session, tenant_id, batch_id, lock=True)
            if batch.kind != "files":
                raise BatchInvalid("A ZIP's batch takes no more files.")
            position = self._next_position(session, batch_id)
            if position >= batch.expected:
                raise BatchInvalid(f"This batch has its {batch.expected} files already.")
            doc_type = batch.doc_type
            # Its place is taken now, so two files sent at once cannot take the same one.
            item = UploadBatchItem(
                tenant_id=tenant_id, batch_id=batch_id, position=position,
                filename=safe_filename(filename) or f"file-{position + 1}", size_bytes=len(data),
                outcome="refused", reason=NOT_ARRIVED,
            )  # fmt: skip
            session.add(item)
            session.flush()
            item_id, name = item.id, item.filename
        outcome, reason, document_id = self._take(
            tenant_id, doc_type, name, data, actor=actor, free=free
        )
        with self._sessions.begin() as session:
            row = session.get_one(UploadBatchItem, item_id)
            row.outcome, row.reason, row.document_id = outcome, reason, document_id
        return self._items(tenant_id, batch_id, only=item_id)[0]

    @scoped
    def from_zip(
        self,
        tenant_id: uuid.UUID,
        source: IO[bytes],
        zip_name: str | None,
        *,
        doc_type: str,
        actor: str,
        person: str,
        free: bool,
    ) -> BatchView:
        """Every file of a ZIP as its own document. Raises `ZipRefused` (nothing made)."""
        self._known_type(doc_type)
        archive = open_zip(source, max_file_bytes=self._max_upload_bytes)
        counts = {"received": 0, "already_received": 0, "refused": 0}
        try:
            with self._sessions.begin() as session:
                batch = UploadBatch(
                    tenant_id=tenant_id, kind="zip", doc_type=doc_type,
                    expected=len(archive.entries), name=safe_filename(zip_name),
                    created_by=actor, person=person,
                )  # fmt: skip
                session.add(batch)
                session.flush()
                batch_id = batch.id
            for entry in archive.entries:
                document_id: uuid.UUID | None = None
                outcome: Outcome = "refused"
                reason: str | None = entry.problem
                if entry.problem is None:
                    outcome, reason, document_id = self._take(
                        tenant_id, doc_type, entry.filename, archive.read(entry),
                        actor=actor, free=free,
                    )  # fmt: skip
                counts[outcome] += 1
                with self._sessions.begin() as session:
                    session.add(
                        UploadBatchItem(
                            tenant_id=tenant_id, batch_id=batch_id, position=entry.position,
                            filename=entry.filename, size_bytes=entry.size, outcome=outcome,
                            reason=reason, document_id=document_id,
                        )
                    )  # fmt: skip
        finally:
            archive.close()
        with self._sessions.begin() as session:
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="batch.created",
                target_type="upload_batch", target_id=str(batch_id),
                details={"kind": "zip", "files": len(archive.entries), **counts},
            )  # fmt: skip
        return self.batch(tenant_id, batch_id)

    def _take(
        self, tenant_id: uuid.UUID, doc_type: str, filename: str, data: bytes, *, actor: str,
        free: bool,
    ) -> tuple[Outcome, str | None, uuid.UUID | None]:  # fmt: skip
        """The upload's checks and entry point; any refusal is this file's alone."""
        try:
            check_file(
                data,
                max_upload_bytes=self._max_upload_bytes,
                max_pages=self._documents.pages_allowed(doc_type, self._max_pages),
            )
            result = self._documents.ingest(
                tenant_id=tenant_id, doc_type=doc_type, filename=filename, data=data,
                actor=actor,
                max_documents=self._caps.max_documents if free else None,
                readings_per_day=self._caps.uploads_per_day if free else None,
                free_readings_per_day=self._caps.readings_per_day_total if free else None,
            )  # fmt: skip
        except FileRefused as error:
            return "refused", error.message, None
        except QueueFull:
            return "refused", QUEUE_FULL, None
        except WorkspaceFull:
            return "refused", WORKSPACE_FULL, None
        except (DailyReadingsUsed, FreeReadingsUsed):
            return "refused", READINGS_USED, None
        except DocumentTypeConflict as error:
            return "refused", f"{str(error).capitalize()}.", None
        except StorageUnavailable:
            return "refused", STORAGE_DOWN, None
        return ("received" if result.created else "already_received"), None, result.document.id

    def _known_type(self, doc_type: str) -> None:
        if doc_type not in self._documents.document_types:
            supported = ", ".join(self._documents.document_types)
            raise BatchInvalid(f"Unknown document type. Supported: {supported}.")

    # --- reading and retrying ----------------------------------------------------------

    @scoped
    def batch(self, tenant_id: uuid.UUID, batch_id: uuid.UUID) -> BatchView:
        """Raises `BatchNotFound`."""
        with self._sessions() as session:
            row = self._row(session, tenant_id, batch_id)
            return self._view(row, self._items(tenant_id, batch_id))

    @scoped
    def batches(self, tenant_id: uuid.UUID, *, limit: int = 20) -> list[BatchView]:
        """The newest batches first."""
        with self._sessions() as session:
            rows = list(
                session.scalars(
                    select(UploadBatch)
                    .where(UploadBatch.tenant_id == tenant_id)
                    .order_by(UploadBatch.created_at.desc(), UploadBatch.id.desc())
                    .limit(limit)
                )
            )
        return [self._view(row, self._items(tenant_id, row.id)) for row in rows]

    @scoped
    def retry(self, tenant_id: uuid.UUID, batch_id: uuid.UUID, *, actor: str) -> BatchView:
        """Read again every document of the batch whose reading failed, and only those."""
        view = self.batch(tenant_id, batch_id)
        retried = 0
        for item in view.items:
            if item.state != "failed" or item.document_id is None:
                continue
            try:
                self._documents.reprocess(
                    tenant_id=tenant_id, document_id=item.document_id, actor=actor
                )
            except (ReprocessInProgress, SplitFile):
                continue
            retried += 1
        with self._sessions.begin() as session:
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="batch.retried",
                target_type="upload_batch", target_id=str(batch_id),
                details={"documents": retried},
            )  # fmt: skip
        return self.batch(tenant_id, batch_id)

    @staticmethod
    def _row(
        session: Session, tenant_id: uuid.UUID, batch_id: uuid.UUID, *, lock: bool = False
    ) -> UploadBatch:
        row = session.scalar(
            select(UploadBatch).where(
                UploadBatch.tenant_id == tenant_id, UploadBatch.id == batch_id
            )
        )
        if row is None:
            raise BatchNotFound(batch_id)
        if lock:  # files arriving at once take their places one at a time
            session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"batch:{batch_id}"))))
        return row

    @staticmethod
    def _next_position(session: Session, batch_id: uuid.UUID) -> int:
        taken = session.scalar(
            select(func.max(UploadBatchItem.position)).where(UploadBatchItem.batch_id == batch_id)
        )
        return 0 if taken is None else taken + 1

    def _items(
        self, tenant_id: uuid.UUID, batch_id: uuid.UUID, *, only: uuid.UUID | None = None
    ) -> list[ItemView]:
        with self._sessions() as session:
            query = (
                select(UploadBatchItem, Document.status)
                .outerjoin(
                    Document,
                    (Document.id == UploadBatchItem.document_id)
                    & (Document.tenant_id == tenant_id)
                    & live(),
                )
                .where(UploadBatchItem.tenant_id == tenant_id, UploadBatchItem.batch_id == batch_id)
                .order_by(UploadBatchItem.position)
            )
            if only is not None:
                query = query.where(UploadBatchItem.id == only)
            rows = session.execute(query).all()
            failed = [i.document_id for i, status in rows if status == "failed" and i.document_id]
            errors = self._errors(session, failed)
        return [
            ItemView(
                id=item.id,
                position=item.position,
                filename=item.filename,
                size_bytes=item.size_bytes,
                outcome=item.outcome,
                reason=item.reason,
                document_id=item.document_id,
                state=_state(item.outcome, status),
                error=errors.get(item.document_id) if item.document_id else None,
            )
            for item, status in rows
        ]

    @staticmethod
    def _errors(session: Session, document_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Why each failed document's newest reading failed."""
        if not document_ids:
            return {}
        rows = session.execute(
            select(DocumentVersion.document_id, DocumentVersion.error)
            .where(DocumentVersion.document_id.in_(document_ids))
            .order_by(DocumentVersion.document_id, DocumentVersion.version_no.desc())
        ).all()
        newest: dict[uuid.UUID, str] = {}
        for document, error in rows:
            if document not in newest and error:
                newest[document] = error
        return newest

    @staticmethod
    def _view(row: UploadBatch, items: list[ItemView]) -> BatchView:
        return BatchView(
            id=row.id, kind=row.kind, name=row.name, doc_type=row.doc_type,
            expected=row.expected, created_by=row.created_by, person=row.person,
            created_at=row.created_at, items=tuple(items),
        )  # fmt: skip
