"""Database helpers."""

from alembic.config import Config
from sqlalchemy.engine import URL

SCRIPT_LOCATION = "docforge:migrations"


def alembic_config(url: URL | str | None = None) -> Config:
    """Alembic config for the packaged migrations.

    `url` overrides `Settings.database_url`; it is passed through `attributes` so a
    password containing `%` is not mangled by ini-file interpolation.
    """
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    if url is not None:
        config.attributes["url"] = url
    return config
