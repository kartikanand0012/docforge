"""Application settings, read from the environment and an optional `.env` file."""

from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Defaults match `docker-compose.yml` and are for local development only."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    database_url: str = "postgresql+psycopg://docforge:docforge@127.0.0.1:5432/docforge"

    s3_endpoint_url: str = "http://127.0.0.1:9000"
    s3_access_key: str = "docforge"
    s3_secret_key: SecretStr = SecretStr("docforge-local-secret")
    s3_bucket: str = "docforge-originals"

    gemini_api_key: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def _require_postgres(cls, value: str) -> str:
        if not value.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a postgresql URL")
        return value

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, value: object) -> object:
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
