"""ORM models for the tables the service reads and writes. Migrations are hand-written."""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    ColumnElement,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    Text,
    literal_column,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, CHAR, JSONB, UUID
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
    kind: Mapped[str] = mapped_column(Text, server_default="organisation")  # or personal
    # Shown in place of `name` when set: a personal workspace's is "<person>'s workspace".
    display_name: Mapped[str | None] = mapped_column(Text)


class Document(Base):
    """One uploaded file. `(tenant_id, sha256)` is unique: the content hash is the identity."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    doc_type: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(CHAR(64))
    storage_key: Mapped[str] = mapped_column(Text)
    # As uploaded. Anything but a PDF is read from the PDF made of it (`rendition_key`).
    media_type: Mapped[str] = mapped_column(Text, server_default="application/pdf")
    filename: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, server_default="received")
    # Where it is now (docforge.stages); the audit trail has how it got there.
    stage: Mapped[str] = mapped_column(Text, server_default="stored")

    @property
    def ready_for_chat(self) -> bool:
        return self.stage == "ready"

    # The version whose chunks search shows: the newest one indexed.
    indexed_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()
    # Set when deleted. The application's role cannot see a deleted document at all (its
    # row-level policy), so `live` in a query is a second guard, not the only one.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def live(document: Any = Document) -> ColumnElement[bool]:
    """The condition that a document (or an alias of it) has not been deleted."""
    return document.deleted_at.is_(None)  # type: ignore[no-any-return]


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
    # The PDF made from an upload that was not one, that this version was read from.
    rendition_key: Mapped[str | None] = mapped_column(Text)
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


# The batch number of a stored certificate of analysis, spelled as its index (migration 0011).
BATCH_NUMBER: ColumnElement[str] = literal_column(
    "((extractions.data -> 'batch_no') ->> 'value')", type_=Text
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
    role: Mapped[str] = mapped_column(Text, server_default="reviewer")  # or admin
    failed_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A shared account (the public demo's): its PIN is public, so it is never locked.
    shared: Mapped[bool] = mapped_column(Boolean, server_default="false")
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    # What `pin_hash` is the hash of: a PIN, or an account's password.
    credential: Mapped[str] = mapped_column(Text, server_default="pin")
    # The owner: may see every workspace's figures and look into one, read-only. Set only
    # as the database owner; the application's role cannot write this column.
    platform_admin: Mapped[bool] = mapped_column(Boolean, server_default="false")


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


class ReviewClaim(Base):
    """Who has a document open for review now: a lease, renewed while their screen is open.
    One per document; ignored once `expires_at` has passed."""

    __tablename__ = "review_claims"

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = _tenant()
    reviewer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reviewers.id"))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
    created_by: Mapped[str | None] = mapped_column(Text)  # who made it on the web, if anyone


class AgentCall(Base):
    """One call an AI agent made through the MCP server: which tool, on what, how it went.
    Never the question, the query or any document text."""

    __tablename__ = "agent_calls"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    key_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("api_keys.id"))
    tool: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)  # organisation, document or collection
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    collection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    outcome: Mapped[str] = mapped_column(Text)  # ok, not_found, limited, invalid, error
    results: Mapped[int] = mapped_column(Integer, server_default="0")
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # for `ask`
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


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


class Webhook(Base):
    """Where events are sent. Its signing secret is derived, never stored."""

    __tablename__ = "webhooks"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    url: Mapped[str] = mapped_column(Text)
    events: Mapped[list[str]] = mapped_column(ARRAY(Text))
    secret_version: Mapped[int] = mapped_column(Integer, server_default="1")
    active: Mapped[bool] = mapped_column(Boolean, server_default=true())
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    secret_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookDelivery(Base):
    """One event for one webhook, with its delivery state."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    webhook_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("webhooks.id"))
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChunkRow(Base):
    """One searchable piece of a document version, with where it is on the page."""

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"))
    document_version_id: Mapped[uuid.UUID] = _version()
    chunk_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    page: Mapped[int] = mapped_column(Integer)
    block_ids: Mapped[list[str]] = mapped_column(ARRAY(Text))
    text: Mapped[str] = mapped_column(Text)
    embedding_model: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(768))
    created_at: Mapped[datetime] = _created_at()


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


class Conversation(Base):
    """A person's chat: questions about one document (`document_id`) or the organisation's."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    owner: Mapped[str] = mapped_column(Text)  # the principal's actor; only they may read it
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    collection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # organisation, document or collection; kept when the document or collection is deleted
    scope: Mapped[str] = mapped_column(Text, server_default="organisation")
    title: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Message(Base):
    """One question and its answer. The question may hold personal data, so it is kept here,
    where it can be deleted, and never in the audit log or a trace."""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id")
    )
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)  # supported, partly_supported, unsupported, not_found
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    dropped_citations: Mapped[int] = mapped_column(Integer, server_default="0")
    dropped_statements: Mapped[int] = mapped_column(Integer, server_default="0")
    # Why a question went unanswered (no_passages, not_in_passages, quotes_not_found,
    # figures_not_in_quotes, model_error), and what is known of it: what was missing, what
    # was read. None when it was answered.
    reason: Mapped[str | None] = mapped_column(Text)
    reason_detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    provider: Mapped[str | None] = mapped_column(Text)  # who answered: gemini, anthropic...
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class Collection(Base):
    """A knowledge base: a named set of an organisation's documents to search and ask within."""

    __tablename__ = "collections"

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default="")
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class CollectionDocument(Base):
    __tablename__ = "collection_documents"

    collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = _tenant()
    added_at: Mapped[datetime] = _created_at()
