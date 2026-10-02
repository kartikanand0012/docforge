"""Application settings, read from the environment and an optional `.env` file."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

DATABASE_DRIVER = "postgresql+psycopg"
_LOCAL_DATABASE_URL = "postgresql+psycopg://docforge:docforge@127.0.0.1:5432/docforge"
_LOCAL_S3_SECRET_KEY = "docforge-local-secret"  # noqa: S105 - local Compose default, not a real secret


class Settings(BaseSettings):
    """Defaults match `docker-compose.yml` and are for local development only."""

    # hide_input_in_errors: a validation error must not print a rejected secret.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", frozen=True, hide_input_in_errors=True
    )

    environment: Literal["local", "production"] = "local"

    # A secret because the URL embeds the password.
    database_url: SecretStr = SecretStr(_LOCAL_DATABASE_URL)

    s3_endpoint_url: str = "http://127.0.0.1:9000"
    s3_access_key: str = "docforge"
    s3_secret_key: SecretStr = SecretStr(_LOCAL_S3_SECRET_KEY)
    s3_bucket: str = "docforge-originals"

    gemini_api_key: SecretStr | None = None
    # Pinned, never a "-latest" alias: a model change must be a deliberate, evaluated change.
    gemini_model: str = "gemini-3.5-flash-lite"

    max_upload_bytes: int = 10 * 1024 * 1024
    max_pages: int = 20

    # `module:function` returning one pipeline per document type; lets a deployment swap
    # the parser or model without changing this package.
    pipeline_factory: str = "docforge.wiring:build_pipelines"

    job_max_attempts: int = 5
    job_retry_wait_seconds: float = 5
    worker_heartbeat_seconds: float = 10
    worker_stalled_after_seconds: float = 30

    @field_validator("database_url")
    @classmethod
    def _require_postgres_psycopg(cls, value: SecretStr) -> SecretStr:
        # The message must not echo the value: it contains the password.
        try:
            driver = make_url(value.get_secret_value()).drivername
        except ArgumentError:
            driver = None
        if driver != DATABASE_DRIVER:
            raise ValueError(f"DATABASE_URL must be a {DATABASE_DRIVER}:// URL")
        return value

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _no_local_defaults_in_production(self) -> Self:
        if self.environment != "production":
            return self
        defaults = {
            "DATABASE_URL": self.database_url.get_secret_value() == _LOCAL_DATABASE_URL,
            "S3_SECRET_KEY": self.s3_secret_key.get_secret_value() == _LOCAL_S3_SECRET_KEY,
        }
        unset = [name for name, is_default in defaults.items() if is_default]
        if unset:
            raise ValueError(f"{', '.join(unset)} still has its local default value")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
