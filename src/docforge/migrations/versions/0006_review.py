"""Review: reviewers, corrections, signed reviews, and the model's reply kept with each extraction.

Revision ID: 0006
Revises: 0005
"""

import os
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALLOW_DOWNGRADE = "DOCFORGE_ALLOW_DESTRUCTIVE_DOWNGRADE"
_UUID = postgresql.UUID(as_uuid=True)
_APPEND_ONLY = ("corrections", "reviews")
_NAME = r"[A-Za-z_][A-Za-z0-9_]*(\[(0|[1-9][0-9]*)\])?"
_PATH = rf"^{_NAME}(\.{_NAME})*$"

# A reviewer is never deleted (signatures name them) and who they are cannot be edited;
# only the PIN counters and deactivation change.
_REVIEWER_GUARD = """
CREATE FUNCTION docforge_reviewer_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'reviewers are deactivated, not deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.email <> OLD.email
        OR NEW.name <> OLD.name OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'a reviewer''s identity cannot be changed'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;
"""

# Once a version is signed nothing may be corrected on it, whoever writes.
_NO_CORRECTION_AFTER_REVIEW = """
CREATE FUNCTION docforge_no_correction_after_review() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM reviews WHERE document_version_id = NEW.document_version_id) THEN
        RAISE EXCEPTION 'version % is signed and cannot be corrected', NEW.document_version_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;
"""


def _tenant() -> sa.Column[Any]:
    return sa.Column(
        "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )


def _created_at(name: str = "created_at") -> sa.Column[Any]:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _version_of(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["document_version_id", "tenant_id"],
        ["document_versions.id", "document_versions.tenant_id"],
        name=f"fk_{table}_version_tenant",
        ondelete="RESTRICT",
    )


def _reviewer_of(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["reviewer_id", "tenant_id"],
        ["reviewers.id", "reviewers.tenant_id"],
        name=f"fk_{table}_reviewer_tenant",
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    # What the model returned, merged across pages: corrections are applied to it and it is
    # read again. Null for extractions made before this migration.
    op.add_column("extractions", sa.Column("raw", postgresql.JSONB, nullable=True))

    op.create_table(
        "reviewers",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("pin_hash", sa.Text, nullable=False),
        sa.Column("failed_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.UniqueConstraint("id", "tenant_id", name="uq_reviewers_id_tenant"),
        sa.CheckConstraint("email = lower(btrim(email))", name="ck_reviewers_email_lower"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_reviewers_name"),
        sa.CheckConstraint("failed_attempts >= 0", name="ck_reviewers_failed_attempts"),
    )
    op.create_index("uq_reviewers_tenant_email", "reviewers", ["tenant_id", "email"], unique=True)

    op.create_table(
        "corrections",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant(),
        sa.Column("document_version_id", _UUID, nullable=False),
        sa.Column("reviewer_id", _UUID, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("old_text", sa.Text, nullable=True),
        sa.Column("new_text", sa.Text, nullable=True),
        sa.Column("reason", sa.Text, nullable=False),
        _created_at(),
        _version_of("corrections"),
        _reviewer_of("corrections"),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0 AND length(reason) <= 2000", name="ck_corrections_reason"
        ),
        sa.CheckConstraint(f"length(path) <= 200 AND path ~ '{_PATH}'", name="ck_corrections_path"),
        sa.CheckConstraint(
            "coalesce(length(old_text), 0) <= 10000 AND coalesce(length(new_text), 0) <= 10000",
            name="ck_corrections_text",
        ),
    )
    op.create_index("ix_corrections_version", "corrections", ["document_version_id", "created_at"])

    op.create_table(
        "reviews",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant(),
        sa.Column("document_version_id", _UUID, nullable=False),
        sa.Column("reviewer_id", _UUID, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("meaning", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("override_reason", sa.Text, nullable=True),
        sa.Column("record_sha256", sa.CHAR(64), nullable=False),
        sa.Column("data", postgresql.JSONB, nullable=False),  # the signed record and any draft
        _created_at("signed_at"),
        _version_of("reviews"),
        _reviewer_of("reviews"),
        sa.UniqueConstraint("document_version_id", name="uq_reviews_version"),
        sa.CheckConstraint("outcome IN ('approved', 'rejected')", name="ck_reviews_outcome"),
        sa.CheckConstraint("length(meaning) > 0 AND length(reason) > 0", name="ck_reviews_text"),
        sa.CheckConstraint("record_sha256 ~ '^[0-9a-f]{64}$'", name="ck_reviews_sha256"),
        sa.CheckConstraint(
            "jsonb_typeof(data->'blockers') = 'array' AND data ? 'record'", name="ck_reviews_data"
        ),
        sa.CheckConstraint(
            "override_reason IS NULL OR length(btrim(override_reason)) > 0",
            name="ck_reviews_override_text",
        ),
        # An approval over open checks always carries the reason it was given.
        sa.CheckConstraint(
            "outcome <> 'approved' OR override_reason IS NOT NULL "
            "OR jsonb_array_length(data->'blockers') = 0",
            name="ck_reviews_override",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["extractions.document_version_id"],
            name="fk_reviews_extraction",
            ondelete="RESTRICT",
        ),
    )
    op.execute(_REVIEWER_GUARD)
    op.execute(
        "CREATE TRIGGER reviewers_guard BEFORE UPDATE OR DELETE ON reviewers "
        "FOR EACH ROW EXECUTE FUNCTION docforge_reviewer_guard()"
    )
    op.execute("ALTER TABLE reviewers ENABLE ALWAYS TRIGGER reviewers_guard")
    op.execute(_NO_CORRECTION_AFTER_REVIEW)
    op.execute(
        "CREATE TRIGGER corrections_not_after_review BEFORE INSERT ON corrections "
        "FOR EACH ROW EXECUTE FUNCTION docforge_no_correction_after_review()"
    )
    op.execute("ALTER TABLE corrections ENABLE ALWAYS TRIGGER corrections_not_after_review")
    for table in _APPEND_ONLY:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.create_index(f"ix_{table}_reviewer", table, ["reviewer_id", "tenant_id"])
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION docforge_forbid_change()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION docforge_forbid_change()"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_append_only")
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_no_truncate")


def downgrade() -> None:
    if os.environ.get(_ALLOW_DOWNGRADE) != "1":
        for table in (*_APPEND_ONLY, "reviewers"):
            query = sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")  # noqa: S608 - fixed names
            if op.get_bind().execute(query).scalar():
                raise RuntimeError(
                    f"{table} has records and this downgrade would discard them; "
                    f"set {_ALLOW_DOWNGRADE}=1 to do it anyway"
                )
        kept = sa.text("SELECT EXISTS (SELECT 1 FROM extractions WHERE raw IS NOT NULL)")
        if op.get_bind().execute(kept).scalar():
            raise RuntimeError(
                "extractions hold model replies this downgrade would discard; "
                f"set {_ALLOW_DOWNGRADE}=1 to do it anyway"
            )
    op.drop_table("reviews")
    op.drop_table("corrections")
    op.drop_table("reviewers")
    op.drop_column("extractions", "raw")
    op.execute("DROP FUNCTION docforge_no_correction_after_review()")
    op.execute("DROP FUNCTION docforge_reviewer_guard()")
