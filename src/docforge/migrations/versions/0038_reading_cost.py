"""What reading documents costs, apart from answering questions, and per page.

- `docforge_platform_usage()` now says of each sum of tokens whether it was spent reading
  documents (`reading`, from `model_runs`) or answering questions (`question`, from
  `question_usage`).
- `docforge_platform_reading_volume()`: how many document versions were read (each once,
  however many calls its reading took) and how many pages they had, deleted documents' too -
  what was spent on them stays spent.

Both stay the platform's alone: SECURITY DEFINER with a pinned search_path, no rights for
PUBLIC, run by the platform login only, and nothing returned unless the platform service said
it checked the caller (the volume, a single aggregate row, is then (0, 0): no figure leaks).

Pages are known for PDFs only (`documents.page_count` is null for other uploads), so a
reading of another format counts as a reading with no pages and the cost per page reads a
little high where many are not PDFs.

Deploy: the migration runs before the API that reads `kind` starts (the admin service first);
an API started earlier fails only the platform overview until it has run.

Revision ID: 0038
Revises: 0037
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLATFORM_ROLE = "docforge_platform"
_ASKED = "current_setting('docforge.platform_admin', true) = 'checked'"
_DEFINER = "LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"

_USAGE = f"""
SELECT * FROM (
    SELECT m.tenant_id, 'reading'::text, m.provider, m.model,
        sum(coalesce(m.input_tokens, 0))::bigint, sum(coalesce(m.output_tokens, 0))::bigint,
        sum(coalesce(m.thinking_tokens, 0))::bigint
    FROM model_runs m
    GROUP BY m.tenant_id, m.provider, m.model
    UNION ALL
    SELECT q.tenant_id, 'question'::text, q.provider, q.model, sum(q.input_tokens)::bigint,
        sum(q.output_tokens)::bigint, 0::bigint
    FROM question_usage q
    GROUP BY q.tenant_id, q.provider, q.model
) spent WHERE {_ASKED}
"""  # noqa: S608 - fixed text, no input in it

_READING_VOLUME = f"""
SELECT count(*)::bigint, coalesce(sum(d.page_count), 0)::bigint
FROM (SELECT DISTINCT m.document_version_id FROM model_runs m) r
JOIN document_versions v ON v.id = r.document_version_id
JOIN documents d ON d.id = v.document_id
WHERE {_ASKED}
"""  # noqa: S608 - fixed text, no input in it

_FUNCTIONS = {
    "docforge_platform_usage()": (
        "RETURNS TABLE (tenant_id uuid, kind text, provider text, model text, "
        "input_tokens bigint, output_tokens bigint, thinking_tokens bigint)",
        _USAGE,
    ),
    "docforge_platform_reading_volume()": (
        "RETURNS TABLE (readings bigint, pages bigint)",
        _READING_VOLUME,
    ),
}


def _create(signature: str, returns: str, body: str) -> None:
    op.execute(f"CREATE FUNCTION {signature} {returns} {_DEFINER} AS $$ {body} $$")
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_PLATFORM_ROLE}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP FUNCTION docforge_platform_usage()")  # its columns change
    for signature, (returns, body) in _FUNCTIONS.items():
        _create(signature, returns, body)


def downgrade() -> None:
    import importlib.util
    from pathlib import Path

    op.execute("SET LOCAL lock_timeout = '5s'")
    for signature in _FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
    # 0036's usage function as it was.
    spec = importlib.util.spec_from_file_location(
        "platform_0036", Path(__file__).with_name("0036_spent_and_paged.py")
    )
    assert spec is not None and spec.loader is not None  # noqa: S101
    before = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(before)
    returns, body = before._FUNCTIONS["docforge_platform_usage()"]
    _create("docforge_platform_usage()", returns, body)
