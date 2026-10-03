"""What the application's database role may not do, beyond row-level security."""

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

APPEND_ONLY = ("audit_log", "extractions", "assessments", "matches", "corrections", "reviews")


def privilege(engine: Engine, check: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text(f"SELECT {check}")).scalar_one())


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_append_only_tables_cannot_even_be_updated_by_privilege(engine: Engine, table: str) -> None:
    assert not privilege(engine, f"has_table_privilege(current_user, '{table}', 'UPDATE')")


def test_organisations_cannot_be_created_or_renamed_by_the_application(engine: Engine) -> None:
    assert privilege(engine, "has_table_privilege(current_user, 'tenants', 'SELECT')")
    assert not privilege(engine, "has_table_privilege(current_user, 'tenants', 'INSERT')")
    assert not privilege(engine, "has_table_privilege(current_user, 'tenants', 'UPDATE')")


@pytest.mark.parametrize("right", ["TRUNCATE", "TRIGGER", "REFERENCES"])
def test_the_queue_tables_cannot_be_truncated_or_given_triggers(engine: Engine, right: str) -> None:
    assert not privilege(
        engine, f"has_table_privilege(current_user, 'procrastinate_jobs', '{right}')"
    )


@pytest.mark.parametrize(
    "function",
    [
        "docforge_forbid_change()",
        "docforge_reviewer_guard()",
        "docforge_no_correction_after_review()",
        "docforge_match_sides()",
    ],
)
def test_trigger_functions_are_not_callable_by_the_application(
    engine: Engine, function: str
) -> None:
    assert not privilege(engine, f"has_function_privilege(current_user, '{function}', 'EXECUTE')")


def test_lookup_functions_search_the_system_catalog_first(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        configs = (
            conn.execute(
                text("SELECT proconfig FROM pg_proc WHERE proname LIKE 'docforge\\_%\\_tenant'")
            )
            .scalars()
            .all()
        )

    assert configs and all(c == ["search_path=pg_catalog, public, pg_temp"] for c in configs)
