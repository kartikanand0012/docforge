"""Application settings, read from the environment and an optional `.env` file."""

import json
import math
import re
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

DATABASE_DRIVER = "postgresql+psycopg"
_FACTORY = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*")
# Robustness and capacity runs: the real parser and converter, the model simulated.
CAPACITY_FACTORY = "docforge.loadtest.pipelines:build_capacity_pipelines"
# The owner, for migrations only, and the restricted login the API and worker use.
_LOCAL_DATABASE_URL = "postgresql+psycopg://docforge:docforge@127.0.0.1:5432/docforge"
_LOCAL_APP_PASSWORD = "docforge-app-local"  # noqa: S105 - local Compose default, not a real secret
_LOCAL_APP_DATABASE_URL = (
    f"postgresql+psycopg://docforge_app_user:{_LOCAL_APP_PASSWORD}@127.0.0.1:5432/docforge"
)
_LOCAL_S3_SECRET_KEY = "docforge-local-secret"  # noqa: S105 - local Compose default, not a real secret
_LOCAL_WEBHOOK_KEY = "docforge-local-webhook-key"
PROVIDERS = ("gemini", "anthropic", "openai")
Task = Literal["extraction", "chat"]


def _price(value: object) -> bool:
    """A real, finite, non-negative number of dollars (true is not a price)."""
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


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

    # Unset (or blank) means AWS S3 itself, with the instance's role for credentials.
    s3_endpoint_url: str | None = "http://127.0.0.1:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str = "docforge"
    s3_secret_key: SecretStr = SecretStr(_LOCAL_S3_SECRET_KEY)
    s3_bucket: str = "docforge-originals"

    gemini_api_key: SecretStr | None = None
    # Pinned, never a "-latest" alias: a model change must be a deliberate, evaluated change.
    gemini_model: str = "gemini-3.5-flash-lite"
    # Pinned for the same reason: vectors from two models cannot be compared.
    embedding_model: str = "gemini-embedding-001"

    # Which provider reads fields from documents, and which answers questions. Gemini unless
    # told otherwise; search's embeddings are Gemini's whatever these say.
    extraction_provider: str = "gemini"
    chat_provider: str = "gemini"
    extraction_model: str | None = None  # unset: the provider's pinned model below
    chat_model: str | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    openai_api_key: SecretStr | None = None
    openai_model: str | None = None  # chosen by the owner and pinned: no default
    openai_reasoning_effort: str | None = None  # a reasoning model only; part of its pin
    # One model call's whole time, its retries and waits included (Claude and OpenAI write
    # long extractions slowly). Each attempt may take all of what is left.
    llm_timeout_seconds: float = Field(default=600.0, gt=0, le=3600)
    # USD per million tokens, input and output, per "provider/model":
    # {"anthropic/claude-sonnet-5-5": [3, 15]}. A model without a price shows no cost.
    model_prices: str = "{}"

    max_upload_bytes: int = 10 * 1024 * 1024
    max_pages: int = 20
    # The parser runs in a child process: replaced after this many documents, and stopped if
    # one document takes longer or more memory than this.
    parser_max_documents: int = 50
    parser_timeout_seconds: float = 900.0
    parser_max_rss_mb: int = 8192
    # Google Drive sync: DocForge's service account key (JSON), shared folders are read with it.
    google_service_account_json: SecretStr | None = None
    drive_client: Literal["google", "fake"] = "google"  # fake: tests and local demos only
    drive_sync_interval_minutes: int = 10
    # The converter service (LibreOffice with no network of its own); without it, conversion
    # runs in a child process of the worker (development and tests).
    converter_url: str | None = None
    converter_token: SecretStr | None = None
    chat_daily_limit: int = 500  # questions per organisation per day (each is a model call)
    chat_daily_limit_per_person: int = 100  # so one person cannot use up the organisation's
    chat_record: bool = False  # record chat replies for replay (with a key; never deployed)
    conversion_timeout_seconds: float = 120.0  # LibreOffice, per office file
    conversion_max_rss_mb: int = 2048  # the converter and every process it starts
    parser_batch_pages: int = 10  # pages converted at a time; bounds memory on long files
    # Use only models already on disk: no download while a document is being parsed.
    # Needs the models present first (`make models`); the deployed image bakes them in.
    parser_offline: bool = False

    # `module:function` returning one pipeline per document type; lets a deployment swap
    # the parser or model without changing this package.
    pipeline_factory: str = "docforge.wiring:build_pipelines"
    # Robustness and capacity runs only: no model is called; every reply is empty and marked
    # simulated, after SIMULATED_MODEL_TIME (zero, c1, fixed:N). Needs the capacity factory.
    simulated_model: bool = False
    simulated_model_time: str = "zero"

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

    @field_validator("s3_endpoint_url", mode="before")
    @classmethod
    def _blank_endpoint_is_aws(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("google_service_account_json", mode="before")
    @classmethod
    def _service_account_key(cls, value: object) -> object:
        if value in (None, ""):
            return None
        try:
            data = json.loads(str(value))
        except ValueError:
            data = None
        # The key itself is never put in an error message.
        if (
            not isinstance(data, dict)
            or data.get("type") != "service_account"
            or not (data.get("client_email") and data.get("private_key"))
        ):
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON must be a service account key (JSON)")
        return value

    @property
    def drive_service_account_email(self) -> str | None:
        """Who a customer shares a folder with; the only part of the key ever shown."""
        if self.google_service_account_json is None:
            return None
        email = json.loads(self.google_service_account_json.get_secret_value())["client_email"]
        return str(email)

    @field_validator("gemini_api_key", "anthropic_api_key", "openai_api_key", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("extraction_model", "chat_model", "openai_model", mode="before")
    @classmethod
    def _blank_model_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("extraction_provider", "chat_provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        if value not in PROVIDERS:
            raise ValueError(f"the provider must be one of {', '.join(PROVIDERS)}")
        return value

    @field_validator(
        "gemini_model", "anthropic_model", "openai_model", "extraction_model", "chat_model"
    )
    @classmethod
    def _pinned(cls, value: str | None) -> str | None:
        # A "-latest" alias moves under us: a model change must be deliberate and evaluated.
        if value is not None and value.endswith("-latest"):
            raise ValueError(f"{value!r} is an alias that moves; pin a model version")
        return value

    @field_validator("model_prices")
    @classmethod
    def _prices(cls, value: str) -> str:
        try:
            prices = json.loads(value or "{}")
            if not isinstance(prices, dict) or not all(
                isinstance(k, str)
                and "/" in k
                and isinstance(v, list)
                and len(v) == 2
                and all(_price(n) for n in v)
                for k, v in prices.items()
            ):
                raise ValueError
        except ValueError:
            raise ValueError(
                'MODEL_PRICES must be {"provider/model": [input, output]} in USD per million'
            ) from None
        return value

    @model_validator(mode="after")
    def _simulated_model_only_by_name(self) -> Self:
        capacity = self.pipeline_factory == CAPACITY_FACTORY
        if capacity != self.simulated_model:
            raise ValueError(
                f"SIMULATED_MODEL=true and PIPELINE_FACTORY={CAPACITY_FACTORY} go together: "
                "set both for a capacity run, or neither"
            )
        return self

    @model_validator(mode="after")
    def _openai_model_is_pinned(self) -> Self:
        if "openai" in (self.extraction_provider, self.chat_provider) and not self.openai_model:
            raise ValueError("OPENAI_MODEL must name the OpenAI model to use, pinned")
        return self

    def provider_for(self, task: Task) -> str:
        return self.extraction_provider if task == "extraction" else self.chat_provider

    def model_for(self, task: Task) -> str:
        """The model `task` uses: its own setting, or its provider's pinned model."""
        chosen = self.extraction_model if task == "extraction" else self.chat_model
        if chosen:
            return chosen
        provider = self.provider_for(task)
        if provider == "anthropic":
            return self.anthropic_model
        if provider == "openai":
            return str(self.openai_model)
        return self.gemini_model

    def prices(self) -> dict[str, tuple[float, float]]:
        """USD per million input and output tokens, by "provider/model"."""
        found = {
            name: (float(pair[0]), float(pair[1]))
            for name, pair in json.loads(self.model_prices or "{}").items()
        }
        # The settings from before several providers: Gemini's price.
        if (
            self.price_input_per_million_usd is not None
            and self.price_output_per_million_usd is not None
        ):
            found.setdefault(
                f"gemini/{self.gemini_model}",
                (self.price_input_per_million_usd, self.price_output_per_million_usd),
            )
        return found

    @model_validator(mode="after")
    def _no_local_defaults_in_production(self) -> Self:
        if self.environment != "production":
            return self
        defaults = {
            "DATABASE_URL": self.database_url.get_secret_value() == _LOCAL_APP_DATABASE_URL,
            "MIGRATION_DATABASE_URL": self.migration_database_url.get_secret_value()
            == _LOCAL_DATABASE_URL,
            # Only for an S3-compatible server: on AWS the instance role is used instead.
            "S3_SECRET_KEY": self.s3_endpoint_url is not None
            and self.s3_secret_key.get_secret_value() == _LOCAL_S3_SECRET_KEY,
            "WEBHOOK_SIGNING_KEY": self.webhook_signing_key.get_secret_value()
            == _LOCAL_WEBHOOK_KEY,
        }
        unset = [name for name, is_default in defaults.items() if is_default]
        if unset:
            raise ValueError(f"{', '.join(unset)} still has its local default value")
        if self.webhook_allow_local:
            raise ValueError("WEBHOOK_ALLOW_LOCAL is for local development only")
        if self.drive_client != "google":
            raise ValueError("DRIVE_CLIENT=fake is for tests and local demos only")
        if self.drive_sync_interval_minutes < 5:
            raise ValueError("DRIVE_SYNC_INTERVAL_MINUTES must be at least 5 in production")
        if self.chat_record:
            # Recording writes questions, passages and answers to disk, unprotected.
            raise ValueError("CHAT_RECORD is for local development only")
        if not self.pipeline_factory.startswith("docforge."):
            # The factory is imported and called with every secret in these settings.
            raise ValueError("PIPELINE_FACTORY must name a function in this package")
        # Last, so the checks above name their own problem first.
        if not self.converter_url:
            raise ValueError("CONVERTER_URL must name the isolated converter service")
        token = self.converter_token.get_secret_value() if self.converter_token else ""
        if len(token) < 32:
            raise ValueError("CONVERTER_TOKEN must be set (32 characters or more)")
        if self.pipeline_factory == "docforge.wiring:build_replay_pipelines":
            return self  # the demo replays recorded replies and embeddings: no provider is called
        if self.simulated_model:
            return self  # a capacity run: no provider is called (ops-check says so)
        keys = {"anthropic": self.anthropic_api_key, "openai": self.openai_api_key}
        for provider in dict.fromkeys((self.extraction_provider, self.chat_provider)):
            if provider in keys and keys[provider] is None:
                raise ValueError(f"{provider.upper()}_API_KEY must be set: {provider} is selected")
        if self.gemini_api_key is None:
            # Search embeds every question with Gemini, whichever provider answers.
            raise ValueError("GEMINI_API_KEY must be set: search embeds with Gemini")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
