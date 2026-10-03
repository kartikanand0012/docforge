"""Application settings, read from the environment and an optional `.env` file."""

import re
from functools import lru_cache
from typing import Literal, Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

DATABASE_DRIVER = "postgresql+psycopg"
_FACTORY = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*")
# The owner, for migrations only, and the restricted login the API and worker use.
_LOCAL_DATABASE_URL = "postgresql+psycopg://docforge:docforge@127.0.0.1:5432/docforge"
_LOCAL_APP_PASSWORD = "docforge-app-local"  # noqa: S105 - local Compose default, not a real secret
_LOCAL_APP_DATABASE_URL = (
    f"postgresql+psycopg://docforge_app_user:{_LOCAL_APP_PASSWORD}@127.0.0.1:5432/docforge"
)
_LOCAL_S3_SECRET_KEY = "docforge-local-secret"  # noqa: S105 - local Compose default, not a real secret
_LOCAL_WEBHOOK_KEY = "docforge-local-webhook-key"


class Settings(BaseSettings):
    """Defaults match `docker-compose.yml` and are for local development only."""

    # hide_input_in_errors: a validation error must not print a rejected secret.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", frozen=True, hide_input_in_errors=True
    )

    environment: Literal["local", "production"] = "local"

    # Secrets because the URLs embed passwords. The API and worker connect as a role that row-
    # level security applies to and that owns nothing; migrations connect as the owner.
    database_url: SecretStr = SecretStr(_LOCAL_APP_DATABASE_URL)
    migration_database_url: SecretStr = SecretStr(_LOCAL_DATABASE_URL)

    s3_endpoint_url: str = "http://127.0.0.1:9000"
    s3_access_key: str = "docforge"
    s3_secret_key: SecretStr = SecretStr(_LOCAL_S3_SECRET_KEY)
    s3_bucket: str = "docforge-originals"

    gemini_api_key: SecretStr | None = None
    # Pinned, never a "-latest" alias: a model change must be a deliberate, evaluated change.
    gemini_model: str = "gemini-3.5-flash-lite"
    # Pinned for the same reason: vectors from two models cannot be compared.
    embedding_model: str = "gemini-embedding-001"

    max_upload_bytes: int = 10 * 1024 * 1024
    max_pages: int = 20
    # The parser runs in a child process: replaced after this many documents, and stopped if
    # one document takes longer or more memory than this.
    parser_max_documents: int = 50
    parser_timeout_seconds: float = 900.0
    parser_max_rss_mb: int = 8192
    parser_batch_pages: int = 10  # pages converted at a time; bounds memory on long files
    # Use only models already on disk: no download while a document is being parsed.
    # Needs the models present first (`make models`); the deployed image bakes them in.
    parser_offline: bool = False

    # `module:function` returning one pipeline per document type; lets a deployment swap
    # the parser or model without changing this package.
    pipeline_factory: str = "docforge.wiring:build_pipelines"

    # Signs webhook deliveries; each webhook's secret is derived from it. Changing it changes
    # every webhook's secret.
    webhook_signing_key: SecretStr = SecretStr(_LOCAL_WEBHOOK_KEY)
    # Only for local development and tests: let webhooks reach http:// and this machine.
    webhook_allow_local: bool = False
    # Failed sign-ins and PINs allowed per client address in five minutes, per API process.
    failed_logins_per_window: int = 20
    # The review screen's origin(s), comma-separated, e.g. http://localhost:3000.
    cors_origins: str = ""
    # Traces are exported over OTLP/HTTP here (e.g. http://collector:4318); unset, none are.
    otel_exporter_otlp_endpoint: str | None = None
    evals_dir: str = "evals/baselines"
    # Recorded parses and model replies, for `docforge.wiring:build_replay_pipelines`.
    recordings_dir: str = "tests/fixtures/recorded"  # the committed reports the eval page shows
    # Model prices in USD per million tokens, for the cost figure. Unset means no cost is
    # shown: a price that is not checked against the provider's current list is not invented.
    price_input_per_million_usd: float | None = None
    price_output_per_million_usd: float | None = None

    job_max_attempts: int = 5
    max_pending_documents: int = 1000  # uploads are refused while this many are waiting
    job_retry_wait_seconds: float = 5
    worker_heartbeat_seconds: float = 10
    worker_stalled_after_seconds: float = 30

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def _require_postgres_psycopg(cls, value: SecretStr) -> SecretStr:
        # The message must not echo the value: it contains the password.
        try:
            driver = make_url(value.get_secret_value()).drivername
        except ArgumentError:
            driver = None
        if driver != DATABASE_DRIVER:
            raise ValueError(f"database URLs must be {DATABASE_DRIVER}:// URLs")
        return value

    @field_validator("pipeline_factory")
    @classmethod
    def _factory_is_module_and_function(cls, value: str) -> str:
        if not _FACTORY.fullmatch(value):
            raise ValueError("PIPELINE_FACTORY must look like module:function")
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
            "DATABASE_URL": self.database_url.get_secret_value() == _LOCAL_APP_DATABASE_URL,
            "MIGRATION_DATABASE_URL": self.migration_database_url.get_secret_value()
            == _LOCAL_DATABASE_URL,
            "S3_SECRET_KEY": self.s3_secret_key.get_secret_value() == _LOCAL_S3_SECRET_KEY,
            "WEBHOOK_SIGNING_KEY": self.webhook_signing_key.get_secret_value()
            == _LOCAL_WEBHOOK_KEY,
        }
        unset = [name for name, is_default in defaults.items() if is_default]
        if unset:
            raise ValueError(f"{', '.join(unset)} still has its local default value")
        if self.webhook_allow_local:
            raise ValueError("WEBHOOK_ALLOW_LOCAL is for local development only")
        if not self.pipeline_factory.startswith("docforge."):
            # The factory is imported and called with every secret in these settings.
            raise ValueError("PIPELINE_FACTORY must name a function in this package")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
