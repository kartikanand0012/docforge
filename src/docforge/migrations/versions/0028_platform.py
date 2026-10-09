"""The platform administrator's view across workspaces: figures and activity, nothing more.

The application's role sees one workspace at a time. These functions run as the owner and
return, across every workspace, only what the platform screens show: counts, sums of model
tokens, names of workspaces and people, and the audit log's actions - never a document's
name or contents, a question, or an audit entry's details. The API calls them only for a
platform administrator's session.

Revision ID: 0028
Revises: 0027
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_DEFINER = "LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"

_WORKSPACES = """
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
    (SELECT count(*) FROM messages m WHERE m.tenant_id = t.id AND m.status <> 'pending'),
    (SELECT count(*) FROM sessions s WHERE s.tenant_id = t.id)
FROM tenants t
LEFT JOIN LATERAL (
    SELECT r.name, r.email FROM reviewers r
    WHERE r.tenant_id = t.id
        AND (r.id = (SELECT a.reviewer_id FROM accounts a WHERE a.tenant_id = t.id)
             OR (t.kind = 'organisation' AND r.role = 'admin'))
    ORDER BY r.created_at, r.id LIMIT 1
) owner ON true
"""

_USAGE = """
SELECT v.tenant_id, m.provider, m.model, sum(coalesce(m.input_tokens, 0))::bigint,
    sum(coalesce(m.output_tokens, 0))::bigint, sum(coalesce(m.thinking_tokens, 0))::bigint
FROM model_runs m
JOIN document_versions v ON v.id = m.document_version_id
JOIN documents d ON d.id = v.document_id
WHERE d.deleted_at IS NULL
GROUP BY v.tenant_id, m.provider, m.model
UNION ALL
SELECT q.tenant_id, q.provider, q.model, sum(coalesce(q.input_tokens, 0))::bigint,
    sum(coalesce(q.output_tokens, 0))::bigint, 0::bigint
FROM messages q
WHERE q.provider IS NOT NULL AND q.model IS NOT NULL
GROUP BY q.tenant_id, q.provider, q.model
"""

_ACCOUNTS = """
SELECT count(*), count(*) FILTER (WHERE created_at >= now() - interval '7 days') FROM accounts
"""

# Labels only: the action, who did it and to what kind of thing - never the details.
_ACTIVITY = """
SELECT a.id, a.occurred_at, a.tenant_id, coalesce(t.display_name, t.name), a.actor,
    CASE split_part(a.actor, ':', 1)
        WHEN 'reviewer' THEN (SELECT r.name FROM reviewers r
            WHERE r.tenant_id = a.tenant_id AND r.id::text = split_part(a.actor, ':', 2))
        WHEN 'key' THEN (SELECT k.name FROM api_keys k
            WHERE k.tenant_id = a.tenant_id AND k.id::text = split_part(a.actor, ':', 2))
    END,
    a.action, a.target_type
FROM audit_log a JOIN tenants t ON t.id = a.tenant_id
WHERE (before_id IS NULL OR a.id < before_id)
    AND (only_tenant IS NULL OR a.tenant_id = only_tenant)
ORDER BY a.id DESC
LIMIT least(greatest(max_rows, 1), 201)
"""

_FUNCTIONS = {
    "docforge_platform_workspaces()": (
        "RETURNS TABLE (tenant_id uuid, organisation text, kind text, created_at timestamptz, "
        "owner_name text, owner_email text, last_active_at timestamptz, documents bigint, "
        "documents_last_7_days bigint, pages bigint, signed bigint, questions bigint, "
        "sign_ins bigint)",
        _WORKSPACES,
    ),
    "docforge_platform_usage()": (
        "RETURNS TABLE (tenant_id uuid, provider text, model text, input_tokens bigint, "
        "output_tokens bigint, thinking_tokens bigint)",
        _USAGE,
    ),
    "docforge_platform_accounts()": (
        "RETURNS TABLE (accounts bigint, signups_last_7_days bigint)",
        _ACCOUNTS,
    ),
    "docforge_platform_activity(before_id bigint, max_rows integer, only_tenant uuid)": (
        "RETURNS TABLE (id bigint, occurred_at timestamptz, tenant_id uuid, organisation text, "
        "actor text, actor_name text, action text, target_type text)",
        _ACTIVITY,
    ),
}


def upgrade() -> None:
    for signature, (returns, body) in _FUNCTIONS.items():
        op.execute(f"CREATE FUNCTION {signature} {returns} {_DEFINER} AS $$ {body} $$")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_APP_ROLE}")


def downgrade() -> None:
    for signature in _FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature.split('(')[0]}")
