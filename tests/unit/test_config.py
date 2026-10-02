import pytest

from docforge.config import Settings

LOCAL_URL = "postgresql+psycopg://docforge:docforge@127.0.0.1:5432/docforge"


def make_settings(**overrides: str) -> Settings:
    # _env_file=None keeps the developer's real .env out of unit tests.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DATABASE_URL",
        "ENVIRONMENT",
        "S3_ENDPOINT_URL",
        "S3_BUCKET",
        "S3_SECRET_KEY",
        "GEMINI_API_KEY",
        "PIPELINE_FACTORY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_point_at_local_compose_services() -> None:
    settings = make_settings()

    assert settings.environment == "local"
    assert settings.database_url.get_secret_value() == LOCAL_URL
    assert settings.s3_endpoint_url == "http://127.0.0.1:9000"
    assert settings.s3_bucket == "docforge-originals"
    assert settings.gemini_api_key is None


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/other")

    assert make_settings().database_url.get_secret_value() == (
        "postgresql+psycopg://u:p@db:5432/other"
    )


def test_secrets_are_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("S3_SECRET_KEY", "not-a-real-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:db-password@db:5432/other")

    settings = make_settings()

    for secret in ("not-a-real-key", "not-a-real-secret", "db-password"):
        assert secret not in repr(settings)
    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == "not-a-real-key"


def test_blank_gemini_key_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")

    assert make_settings().gemini_api_key is None


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///local.db",
        "postgresql://u:p@db:5432/other",  # would silently pick the psycopg2 driver
        "postgresqlx+psycopg://u:p@db:5432/other",
        "not a url",
    ],
)
def test_rejects_database_urls_that_are_not_postgres_psycopg(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", url)

    with pytest.raises(ValueError, match=r"postgresql\+psycopg"):
        make_settings()


def test_validation_error_does_not_echo_the_database_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:db-password@db:5432/other")

    with pytest.raises(ValueError) as error:
        make_settings()

    assert "db-password" not in str(error.value)


def test_production_refuses_the_local_default_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")

    with pytest.raises(ValueError, match="local default"):
        make_settings()


def test_production_accepts_explicit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://app:s3cr3t@db.internal:5432/docforge")
    monkeypatch.setenv("S3_SECRET_KEY", "a-real-secret")

    assert make_settings().environment == "production"


@pytest.mark.parametrize("value", ["no-colon", ":function", "module:", "a b:c"])
def test_pipeline_factory_must_name_a_module_and_a_function(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PIPELINE_FACTORY", value)

    with pytest.raises(ValueError, match="module:function"):
        make_settings()


def test_production_only_loads_pipelines_from_this_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://app:s3cr3t@db.internal:5432/docforge")
    monkeypatch.setenv("S3_SECRET_KEY", "a-real-secret")
    monkeypatch.setenv("PIPELINE_FACTORY", "somewhere_else:build")

    with pytest.raises(ValueError, match="PIPELINE_FACTORY"):
        make_settings()
