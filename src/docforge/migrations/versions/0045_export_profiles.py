"""V2 P3: import-file format profiles, and what each export run booked.

- `export_profiles`: a workspace's import layouts for its books (Marg), at most one active.
- `export_profile_versions`: each saved layout, never changed: an edit is a new version, and
  a run names the version it used, so its file can be made again as it was.
- `export_run_documents`: the bills each run that made a file booked, so a later run can
  warn that some of its bills were exported before.
- `export_runs` gains its `format` (the mapped CSV of P1, or a Marg purchase import made by
  a profile version), that version, and the earlier runs it overlaps.

Row-level security like every workspace table; erased with the account.

Revision ID: 0045
Revises: 0044
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_ERASE_ANCHOR = "DELETE FROM export_runs WHERE tenant_id = t;"
_ERASE_NEW = (
    "DELETE FROM export_run_documents WHERE tenant_id = t;\n"
    "    DELETE FROM export_runs WHERE tenant_id = t;\n"
    "    DELETE FROM export_profile_versions WHERE tenant_id = t;\n"
    "    DELETE FROM export_profiles WHERE tenant_id = t;"
)
_GRANTS = {
    "export_profiles": "SELECT, INSERT, UPDATE",  # never removed: past runs name a version
    "export_profile_versions": "SELECT, INSERT",  # a version is never changed
    "export_run_documents": "SELECT, INSERT",
}


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


def _who() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def _key() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _tenant() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "export_profiles",
        _key(),
        _tenant(),
        sa.Column("target", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("latest_version", sa.Integer, nullable=False),
        *_who(),
        sa.Column("updated_by", sa.Text),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("target IN ('marg')", name="ck_export_profiles_target"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 200", name="ck_export_profiles_name"),
        sa.CheckConstraint("latest_version >= 1", name="ck_export_profiles_version"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_export_profiles_name"),
    )
    op.create_index(
        "ux_export_profiles_active",
        "export_profiles",
        ["tenant_id", "target"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "export_profile_versions",
        _key(),
        _tenant(),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("export_profiles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("spec", postgresql.JSONB, nullable=False),
        sa.Column("basis", sa.Text, nullable=False),
        *_who(),
        sa.CheckConstraint("version_no >= 1", name="ck_export_profile_versions_no"),
        sa.CheckConstraint(
            "basis IN ('sample', 'upload', 'manual')", name="ck_export_profile_versions_basis"
        ),
        sa.UniqueConstraint("profile_id", "version_no", name="uq_export_profile_versions_no"),
    )
    op.create_index("ix_export_profile_versions_tenant", "export_profile_versions", ["tenant_id"])

    op.add_column(
        "export_runs", sa.Column("format", sa.Text, nullable=False, server_default="mapped_csv")
    )
    op.add_column(
        "export_runs",
        sa.Column(
            "profile_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "export_profile_versions.id",
                ondelete="RESTRICT",
                name="fk_export_runs_profile_version",
            ),
        ),
    )
    op.add_column("export_runs", sa.Column("overlaps", postgresql.JSONB))
    op.create_check_constraint(
        "ck_export_runs_format", "export_runs", "format IN ('mapped_csv', 'marg_import')"
    )
    op.create_check_constraint(
        "ck_export_runs_profile",
        "export_runs",
        "(format = 'marg_import') = (profile_version_id IS NOT NULL) "
        "AND (format <> 'marg_import' OR target = 'marg')",
    )
    op.create_index(
        "ix_export_runs_profile_version",
        "export_runs",
        ["profile_version_id"],
        postgresql_where=sa.text("profile_version_id IS NOT NULL"),
    )

    op.create_table(
        "export_run_documents",
        _tenant(),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("export_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # No foreign key to documents: the record of what a run booked outlives its bills.
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "document_id", name="pk_export_run_documents"),
    )
    op.create_index(
        "ix_export_run_documents_document", "export_run_documents", ["tenant_id", "document_id"]
    )

    for table, grants in _GRANTS.items():
        op.execute(f"REVOKE ALL ON {table} FROM {_APP_ROLE}")
        op.execute(f"GRANT {grants} ON {table} TO {_APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} TO {_APP_ROLE} "
            f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
        )
    _erase_function(_ERASE_ANCHOR, _ERASE_NEW)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _erase_function(_ERASE_NEW, _ERASE_ANCHOR)
    op.drop_table("export_run_documents")
    op.drop_index("ix_export_runs_profile_version", table_name="export_runs")
    op.drop_constraint("ck_export_runs_profile", "export_runs", type_="check")
    op.drop_constraint("ck_export_runs_format", "export_runs", type_="check")
    op.drop_constraint("fk_export_runs_profile_version", "export_runs", type_="foreignkey")
    op.drop_column("export_runs", "overlaps")
    op.drop_column("export_runs", "profile_version_id")
    op.drop_column("export_runs", "format")
    op.drop_table("export_profile_versions")
    op.drop_table("export_profiles")
