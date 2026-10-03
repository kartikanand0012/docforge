"""ORM models for the tables the service reads and writes. Migrations are hand-written."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    Text,
    literal_column,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_NEW_UUID = text("gen_random_uuid()")
_NOW = text("now()")


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=_NEW_UUID)


def _tenant() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))


def _version() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("document_versions.id"))


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=_NOW)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _created_at()


class Document(Base):
    """One uploaded file. `(tenant_id, sha256)` is unique: the content hash is the identity."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    doc_type: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(CHAR(64))
    storage_key: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, server_default="received")
    created_at: Mapped[datetime] = _created_at()


class DocumentVersion(Base):
    """One processing run of a document."""

    __tablename__ = "document_versions"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    parser_version: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    model_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class ParseOutput(Base):
    __tablename__ = "parse_outputs"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    document_version_id: Mapped[uuid.UUID] = _version()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class Extraction(Base):
    """Immutable: the database rejects UPDATE and DELETE."""

    __tablename__ = "extractions"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    document_version_id: Mapped[uuid.UUID] = _version()
    schema_version: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(CHAR(64))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # the model's reply; C5 onward
    created_at: Mapped[datetime] = _created_at()


class ModelRun(Base):
    __tablename__ = "model_runs"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    document_version_id: Mapped[uuid.UUID] = _version()
    call_no: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    thinking_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = _created_at()


# The order number inside a stored extraction. Spelled exactly as in the expression index
# `ix_extractions_po_no` (migration 0005); a different spelling would not use the index.
ORDER_NUMBER: ColumnElement[str] = literal_column(
    "((extractions.data -> 'po_no') ->> 'value')", type_=Text
)


class AssessmentRecord(Base):
    """The checks run on one version and their outcome. Immutable, like the extraction."""

    __tablename__ = "assessments"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    document_version_id: Mapped[uuid.UUID] = _version()
    decision: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class MatchRecord(Base):
    """One comparison of an invoice version with a purchase-order version. Immutable."""

    __tablename__ = "matches"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    invoice_version_id: Mapped[uuid.UUID] = _version()
    order_version_id: Mapped[uuid.UUID] = _version()
    decision: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class Reviewer(Base):
    """A person who corrects and signs. The PIN is re-entered for every signature."""

    __tablename__ = "reviewers"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)
    pin_hash: Mapped[str] = mapped_column(Text)
    failed_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class Correction(Base):
    """One field's printed text changed or confirmed by a reviewer. Immutable."""

    __tablename__ = "corrections"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    document_version_id: Mapped[uuid.UUID] = _version()
    reviewer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviewers.id"))
    path: Mapped[str] = mapped_column(Text)
    old_text: Mapped[str | None] = mapped_column(Text)
    new_text: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Review(Base):
    """The signed decision on one version. Immutable; one per version."""

    __tablename__ = "reviews"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    document_version_id: Mapped[uuid.UUID] = _version()
    reviewer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviewers.id"))
    outcome: Mapped[str] = mapped_column(Text)
    meaning: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    override_reason: Mapped[str | None] = mapped_column(Text)
    record_sha256: Mapped[str] = mapped_column(CHAR(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    signed_at: Mapped[datetime] = _created_at()


class ApiKey(Base):
    """A credential for a system. Only a hash of its secret is kept."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    prefix: Mapped[str] = mapped_column(CHAR(12))
    digest: Mapped[str] = mapped_column(CHAR(64))
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionToken(Base):
    """A reviewer signed in to the review screen. Only a hash of the token is kept."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    reviewer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviewers.id"))
    prefix: Mapped[str] = mapped_column(CHAR(12))
    digest: Mapped[str] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEntry(Base):
    """Append-only and hash-chained per tenant; see `docforge.audit`."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = _tenant()
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prev_hash: Mapped[str | None] = mapped_column(CHAR(64))
    hash: Mapped[str] = mapped_column(CHAR(64))
