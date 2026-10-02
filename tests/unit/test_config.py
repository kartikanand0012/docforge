import pytest

from docforge.config import Settings


def make_settings(**overrides: str) -> Settings:
    # _env_file=None keeps the developer's real .env out of unit tests.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_point_at_local_compose_services(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "S3_ENDPOINT_URL", "S3_BUCKET", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    settings = make_settings()

    assert settings.database_url == "postgresql+psycopg://docforge:docforge@127.0.0.1:5432/docforge"
    assert settings.s3_endpoint_url == "http://127.0.0.1:9000"
    assert settings.s3_bucket == "docforge-originals"
    assert settings.gemini_api_key is None


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/other")

    assert make_settings().database_url == "postgresql+psycopg://u:p@db:5432/other"


def test_secrets_are_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("S3_SECRET_KEY", "not-a-real-secret")

    settings = make_settings()

    assert "not-a-real-key" not in repr(settings)
    assert "not-a-real-secret" not in repr(settings)
    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == "not-a-real-key"


def test_rejects_non_postgres_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///local.db")

    with pytest.raises(ValueError, match="postgresql"):
        make_settings()
