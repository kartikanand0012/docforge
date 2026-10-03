"""Tighter rights for the application's role.

Revision ID: 0010
Revises: 0009

- No UPDATE on the append-only tables at all: the triggers already refuse it, but a right the
  role does not hold cannot be used if a trigger is ever dropped.
- Organisations are read, not created or renamed, by the application (`python -m
  docforge.admin create-tenant` uses the owner's connection).
- The queue's tables: rows only; no TRUNCATE, TRIGGER or REFERENCES.
- The trigger functions are not callable directly by anyone but the owner (triggers fire
  without that right).
- The lookup functions search the system catalog first.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = "docforge_app"
_APPEND_ONLY = ("audit_log", "extractions", "assessments", "matches", "corrections", "reviews")
_QUEUE_TABLES = (
    "procrastinate_jobs",
    "procrastinate_events",
    "procrastinate_periodic_defers",
    "procrastinate_workers",
)
_TRIGGER_FUNCTIONS = (
    "docforge_forbid_change()",
    "docforge_reviewer_guard()",
    "docforge_no_correction_after_review()",
    "docforge_match_sides()",
)
_LOOKUPS = (
    "docforge_api_key_tenant(text)",
    "docforge_session_tenant(text)",
    "docforge_version_tenant(uuid)",
)


def _existing(table: str) -> bool:
    from sqlalchemy import text

    query = text("SELECT to_regclass(:name) IS NOT NULL")
    return bool(op.get_bind().execute(query, {"name": table}).scalar())


def upgrade() -> None:
    for table in _APPEND_ONLY:
        op.execute(f"REVOKE UPDATE ON {table} FROM {_ROLE}")
    op.execute(f"REVOKE INSERT, UPDATE ON tenants FROM {_ROLE}")
    for table in _QUEUE_TABLES:
        if _existing(table):
            op.execute(f"REVOKE ALL ON {table} FROM {_ROLE}")
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_ROLE}")
    for function in _TRIGGER_FUNCTIONS:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {function} FROM PUBLIC, {_ROLE}")
    for function in _LOOKUPS:
        op.execute(f"ALTER FUNCTION {function} SET search_path = pg_catalog, public, pg_temp")


def downgrade() -> None:
    for function in _LOOKUPS:
        op.execute(f"ALTER FUNCTION {function} SET search_path = public, pg_temp")
    for function in _TRIGGER_FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO PUBLIC")
    for table in _QUEUE_TABLES:
        if _existing(table):
            op.execute(f"GRANT ALL ON {table} TO {_ROLE}")
    op.execute(f"GRANT INSERT, UPDATE ON tenants TO {_ROLE}")
    for table in _APPEND_ONLY:
        op.execute(f"GRANT UPDATE ON {table} TO {_ROLE}")
