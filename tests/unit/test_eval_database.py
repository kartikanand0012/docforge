"""Temporary eval databases are made only on a local Postgres, whichever URL is remote."""

import pytest

from docforge.config import Settings
from docforge.evals import database


def test_a_remote_owner_is_refused_even_when_a_local_target_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = Settings(
        migration_database_url="postgresql+psycopg://owner:pw@db.example.com:5432/docforge"
    )
    monkeypatch.setattr(database, "get_settings", lambda: remote)

    with (
        pytest.raises(RuntimeError, match="local"),
        database.temporary_database(
            "x", "postgresql+psycopg://owner:pw@127.0.0.1:5432/docforge_eval"
        ),
    ):
        pass
