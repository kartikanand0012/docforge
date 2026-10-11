"""Tokens spent stay spent, and the platform overview comes a page at a time.

- `question_usage` keeps what answering each question spent (provider, model, tokens; no
  question, no answer), so a workspace's cost does not drop when a conversation is deleted.
  The application adds rows and reads its own workspace's; it cannot change or delete them.
  Only an account's deletion removes a workspace's (`docforge_delete_account` now does).
  Answers already given are copied in from their messages.
- The platform's figures (0028, behind the platform's own login since 0030):
  `docforge_platform_workspaces(max_rows, skip)` returns one page, most recently active
  first; `docforge_platform_totals()` the figures over every workspace; a workspace's
  questions are counted from the audit log (kept when a conversation is deleted) and its
  model cost from every reading and answer, deleted documents' and conversations' too.
  Each still returns nothing unless the platform service said it checked the caller.

Revision ID: 0036
Revises: 0035
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_PLATFORM_ROLE = "docforge_platform"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_ASKED = "current_setting('docforge.platform_admin', true) = 'checked'"
_DEFINER = "LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"
_ERASE_ANCHOR = "DELETE FROM conversations WHERE tenant_id = t;"
_ERASE_USAGE = "DELETE FROM question_usage WHERE tenant_id = t;"

_COLUMNS = (
    "tenant_id uuid, organisation text, kind text, created_at timestamptz, owner_name text, "
    "owner_email text, last_active_at timestamptz, documents bigint, "
    "documents_last_7_days bigint, pages bigint, signed bigint, questions bigint, "
    "sign_ins bigint"
)
_WORKSPACES_PAGED = f"""
SELECT * FROM (
    SELECT t.id, coalesce(t.display_name, t.name), t.kind, t.created_at,
        owner.name, owner.email,
        -- The newest audit entry or sign-in (sign-ins are not audited).
        greatest((SELECT max(a.occurred_at) FROM audit_log a WHERE a.tenant_id = t.id),
                 (SELECT max(s.created_at) FROM sessions s WHERE s.tenant_id = t.id)),
        (SELECT count(*) FROM documents d WHERE d.tenant_id = t.id AND d.deleted_at IS NULL),
        (SELECT count(*) FROM documents d WHERE d.tenant_id = t.id AND d.deleted_at IS NULL
            AND d.created_at >= now() - interval '7 days'),
        (SELECT coalesce(sum(d.page_count), 0) FROM documents d
            WHERE d.tenant_id = t.id AND d.deleted_at IS NULL),
        (SELECT count(*) FROM reviews r
            JOIN document_versions v ON v.id = r.document_version_id
            JOIN documents d ON d.id = v.document_id
            WHERE r.tenant_id = t.id AND d.deleted_at IS NULL),
        (SELECT count(*) FROM audit_log a
            WHERE a.tenant_id = t.id AND a.action = 'chat.question_asked'),
        (SELECT count(*) FROM sessions s WHERE s.tenant_id = t.id)
    FROM tenants t
    LEFT JOIN LATERAL (
        SELECT r.name, r.email FROM reviewers r
        WHERE r.tenant_id = t.id
            AND (r.id = (SELECT a.reviewer_id FROM accounts a WHERE a.tenant_id = t.id)
                 OR (t.kind = 'organisation' AND r.role = 'admin'))
        ORDER BY r.created_at, r.id LIMIT 1
    ) owner ON true
    WHERE {_ASKED}
) w (tenant_id, organisation, kind, created_at, owner_name, owner_email, last_active_at,
     documents, documents_last_7_days, pages, signed, questions, sign_ins)
ORDER BY coalesce(w.last_active_at, w.created_at) DESC, w.created_at DESC, w.tenant_id
LIMIT least(greatest(max_rows, 1), 500) OFFSET greatest(skip, 0)
"""  # noqa: S608 - fixed text, no input in it

_TOTALS = f"""
SELECT (SELECT count(*) FROM tenants),
    (SELECT count(*) FROM documents WHERE deleted_at IS NULL),
    (SELECT count(*) FROM documents WHERE deleted_at IS NULL
        AND created_at >= now() - interval '7 days'),
    (SELECT coalesce(sum(page_count), 0) FROM documents WHERE deleted_at IS NULL),
    (SELECT count(*) FROM reviews r
        JOIN document_versions v ON v.id = r.document_version_id
        JOIN documents d ON d.id = v.document_id WHERE d.deleted_at IS NULL),
    (SELECT count(*) FROM audit_log WHERE action = 'chat.question_asked')
WHERE {_ASKED}
"""  # noqa: S608 - fixed text, no input in it

# Every reading and every answer: tokens spent on what was later deleted stay spent.
_USAGE = f"""
SELECT * FROM (
    SELECT m.tenant_id, m.provider, m.model, sum(coalesce(m.input_tokens, 0))::bigint,
        sum(coalesce(m.output_tokens, 0))::bigint, sum(coalesce(m.thinking_tokens, 0))::bigint
    FROM model_runs m
    GROUP BY m.tenant_id, m.provider, m.model
    UNION ALL
    SELECT q.tenant_id, q.provider, q.model, sum(q.input_tokens)::bigint,
        sum(q.output_tokens)::bigint, 0::bigint
    FROM question_usage q
    GROUP BY q.tenant_id, q.provider, q.model
) spent WHERE {_ASKED}
"""  # noqa: S608 - fixed text, no input in it

_FUNCTIONS = {
    "docforge_platform_workspaces(max_rows integer, skip integer)": (
        f"RETURNS TABLE ({_COLUMNS})",
        _WORKSPACES_PAGED,
    ),
    "docforge_platform_totals()": (
        "RETURNS TABLE (workspaces bigint, documents bigint, documents_last_7_days bigint, "
        "pages bigint, signed bigint, questions bigint)",
        _TOTALS,
    ),
    "docforge_platform_usage()": (
        "RETURNS TABLE (tenant_id uuid, provider text, model text, input_tokens bigint, "
        "output_tokens bigint, thinking_tokens bigint)",
        _USAGE,
    ),
}
_UNPAGED = "docforge_platform_workspaces()"


def _erase_function(before: str, after: str) -> None:
    """`docforge_delete_account` as it is, with one statement changed."""
    definition = (
        op.get_bind()
        .execute(sa.text("SELECT pg_get_functiondef('docforge_delete_account'::regproc)"))
        .scalar_one()
    )
    if before not in definition:
        raise RuntimeError("docforge_delete_account is not as migration 0033 made it")
    op.execute(sa.text(definition.replace(before, after, 1)))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "question_usage",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_question_usage_tenant", "question_usage", ["tenant_id", "created_at"])
    op.execute(f"REVOKE ALL ON question_usage FROM {_APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON question_usage TO {_APP_ROLE}")
    op.execute("ALTER TABLE question_usage ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY question_usage_tenant ON question_usage TO {_APP_ROLE} "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )
    op.execute(
        "INSERT INTO question_usage "
        "(tenant_id, provider, model, input_tokens, output_tokens, created_at) "
        "SELECT tenant_id, provider, model, coalesce(input_tokens, 0), "
        "coalesce(output_tokens, 0), created_at FROM messages "
        "WHERE provider IS NOT NULL AND model IS NOT NULL ORDER BY created_at"
    )
    _erase_function(_ERASE_ANCHOR, f"{_ERASE_USAGE}\n    {_ERASE_ANCHOR}")

    op.execute(f"DROP FUNCTION {_UNPAGED}")
    op.execute("DROP FUNCTION docforge_platform_usage()")
    for signature, (returns, body) in _FUNCTIONS.items():
        op.execute(f"CREATE FUNCTION {signature} {returns} {_DEFINER} AS $$ {body} $$")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_PLATFORM_ROLE}")


def downgrade() -> None:
    import importlib.util
    from pathlib import Path

    op.execute("SET LOCAL lock_timeout = '5s'")
    for signature in _FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
    # 0028's functions as they were, with 0030's rights.
    spec = importlib.util.spec_from_file_location(
        "platform_0028", Path(__file__).with_name("0028_platform.py")
    )
    assert spec is not None and spec.loader is not None  # noqa: S101
    before = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(before)
    for signature in (_UNPAGED, "docforge_platform_usage()"):
        returns, body = before._FUNCTIONS[signature]
        guarded = f"SELECT * FROM ({body}) found WHERE {_ASKED}"  # noqa: S608 - fixed text
        op.execute(f"CREATE FUNCTION {signature} {returns} {_DEFINER} AS $$ {guarded} $$")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_PLATFORM_ROLE}")
    _erase_function(f"{_ERASE_USAGE}\n    {_ERASE_ANCHOR}", _ERASE_ANCHOR)
    op.drop_table("question_usage")
