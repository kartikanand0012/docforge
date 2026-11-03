"""V2 P5: invoices by email.

- `documents.source`: how a document arrived - `upload` (every one before this) or `email`.
- `intake_addresses`: a workspace's forwarding address; only its token's SHA-256 is kept.
  At most one active per workspace; a revoked one stays, so mail to it is known and refused.
- `intake_senders`: the workspace's allowlist (whole addresses and `@domains`).
- `intake_settings`: per workspace - trust a sender once accepted (off), documents an hour
  per sender (100).
- `intake_messages`, `intake_items`: each mail received and each of its attachments, with what
  became of it (and the held copy's key while a reviewer decides).
- `docforge_intake_address(token_hash)`: the one lookup across workspaces - a token's
  workspace and whether its address is active - for mail, which arrives without a workspace.

Row-level security on each table; erased with the account (held copies under `intake/` with
the workspace's files). Reversible.

Revision ID: 0046
Revises: 0045
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_ERASE_ANCHOR = "DELETE FROM export_profiles WHERE tenant_id = t;"
_ERASE_NEW = (
    "DELETE FROM intake_items WHERE tenant_id = t;\n"
    "    DELETE FROM intake_messages WHERE tenant_id = t;\n"
    "    DELETE FROM intake_senders WHERE tenant_id = t;\n"
    "    DELETE FROM intake_settings WHERE tenant_id = t;\n"
    "    DELETE FROM intake_addresses WHERE tenant_id = t;"
)
_GRANTS = {
    "intake_addresses": "SELECT, INSERT, UPDATE",
    "intake_senders": "SELECT, INSERT, DELETE",
    "intake_settings": "SELECT, INSERT, UPDATE",
    "intake_messages": "SELECT, INSERT, UPDATE",
    "intake_items": "SELECT, INSERT, UPDATE",
}
_LOOKUP = "docforge_intake_address(token_hash text)"
_LOOKUP_BODY = (
    "SELECT a.tenant_id, a.id, a.active FROM intake_addresses a "
    "WHERE a.token_hash = docforge_intake_address.token_hash"
)
_ITEM_OUTCOMES = ("received", "already_received", "ignored", "refused", "held", "discarded")


def _erase_function(before: str, after: str) -> None:
    """`docforge_delete_account` as it is, with one statement changed."""
    definition = (
        op.get_bind()
        .execute(sa.text("SELECT pg_get_functiondef('docforge_delete_account'::regproc)"))
        .scalar_one()
    )
    if before not in definition:
        raise RuntimeError(f"docforge_delete_account does not hold {before!r}")
    op.execute(sa.text(definition.replace(before, after, 1)))


def _key() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _tenant(primary: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        primary_key=primary,
    )


def _when(name: str, *, nullable: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.func.now(),
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # A constant default: no table rewrite; every document so far was an upload.
    op.add_column(
        "documents", sa.Column("source", sa.Text, nullable=False, server_default="upload")
    )
    op.create_check_constraint("ck_documents_source", "documents", "source IN ('upload', 'email')")
    op.create_index(
        "ix_documents_source",
        "documents",
        ["tenant_id", "source", sa.text("created_at DESC"), sa.text("id DESC")],
    )

    op.create_table(
        "intake_addresses",
        _key(),
        _tenant(),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("token_hash", postgresql.CHAR(64), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Text, nullable=False),
        _when("created_at"),
        sa.Column("revoked_by", sa.Text),
        _when("revoked_at", nullable=True),
        sa.CheckConstraint("length(slug) BETWEEN 1 AND 30", name="ck_intake_addresses_slug"),
        sa.CheckConstraint("active = (revoked_at IS NULL)", name="ck_intake_addresses_revoked"),
        sa.UniqueConstraint("token_hash", name="uq_intake_addresses_token"),
    )
    op.create_index(
        "ux_intake_addresses_active",
        "intake_addresses",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.create_table(
        "intake_senders",
        _key(),
        _tenant(),
        sa.Column("pattern", sa.Text, nullable=False),  # a@b.in, or @b.in for a domain
        sa.Column("created_by", sa.Text, nullable=False),
        _when("created_at"),
        sa.CheckConstraint(
            "length(pattern) BETWEEN 3 AND 254 AND pattern = lower(pattern)",
            name="ck_intake_senders_pattern",
        ),
        sa.UniqueConstraint("tenant_id", "pattern", name="uq_intake_senders_pattern"),
    )

    op.create_table(
        "intake_settings",
        _tenant(primary=True),
        sa.Column("trust_accepted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("per_sender_hourly", sa.Integer, nullable=False, server_default="100"),
        sa.Column("updated_by", sa.Text),
        _when("updated_at", nullable=True),
        sa.CheckConstraint(
            "per_sender_hourly BETWEEN 1 AND 10000", name="ck_intake_settings_hourly"
        ),
    )

    op.create_table(
        "intake_messages",
        _key(),
        _tenant(),
        sa.Column(
            "address_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("intake_addresses.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sender", sa.Text, nullable=False),  # lower case
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("provider_message_id", sa.Text),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),  # why it was held or rejected
        sa.Column("decided_by", sa.Text),
        _when("decided_at", nullable=True),
        _when("received_at"),
        sa.CheckConstraint(
            "status IN ('received', 'held', 'rejected', 'accepted', 'discarded')",
            name="ck_intake_messages_status",
        ),
        sa.CheckConstraint("length(subject) <= 300", name="ck_intake_messages_subject"),
    )
    op.create_index(
        "ix_intake_messages_received",
        "intake_messages",
        ["tenant_id", sa.text("received_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_intake_messages_sender", "intake_messages", ["tenant_id", "sender", "received_at"]
    )

    outcomes = ", ".join(f"'{outcome}'" for outcome in _ITEM_OUTCOMES)
    op.create_table(
        "intake_items",
        _key(),
        _tenant(),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("intake_messages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("sha256", postgresql.CHAR(64)),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        # No foreign key: an intake record outlives a deleted document.
        sa.Column("document_id", postgresql.UUID(as_uuid=True)),
        sa.Column("held_key", sa.Text),  # the held copy, while a reviewer decides
        _when("created_at"),
        _when("updated_at", nullable=True),
        sa.CheckConstraint(f"outcome IN ({outcomes})", name="ck_intake_items_outcome"),
        sa.CheckConstraint(
            "(outcome = 'held') = (held_key IS NOT NULL)", name="ck_intake_items_held"
        ),
        sa.UniqueConstraint("message_id", "position", name="uq_intake_items_position"),
    )
    op.create_index("ix_intake_items_message", "intake_items", ["tenant_id", "message_id"])
    op.create_index(
        "ix_intake_items_document",
        "intake_items",
        ["tenant_id", "document_id"],
        postgresql_where=sa.text("document_id IS NOT NULL"),
    )

    for table, grants in _GRANTS.items():
        op.execute(f"REVOKE ALL ON {table} FROM {_APP_ROLE}")
        op.execute(f"GRANT {grants} ON {table} TO {_APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} TO {_APP_ROLE} "
            f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
        )
    op.execute(
        f"CREATE FUNCTION {_LOOKUP} RETURNS TABLE (tenant_id uuid, address_id uuid, "
        "active boolean) LANGUAGE sql STABLE SECURITY DEFINER "
        f"SET search_path = public, pg_temp AS $$ {_LOOKUP_BODY} $$"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_LOOKUP} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_LOOKUP} TO {_APP_ROLE}")
    _erase_function(_ERASE_ANCHOR, f"{_ERASE_ANCHOR}\n    {_ERASE_NEW}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _erase_function(f"{_ERASE_ANCHOR}\n    {_ERASE_NEW}", _ERASE_ANCHOR)
    op.execute(f"DROP FUNCTION {_LOOKUP.split('(')[0]}")
    for table in ("intake_items", "intake_messages", "intake_settings", "intake_senders",
                  "intake_addresses"):  # fmt: skip
        op.drop_table(table)
    op.drop_index("ix_documents_source", table_name="documents")
    op.drop_constraint("ck_documents_source", "documents", type_="check")
    op.drop_column("documents", "source")
