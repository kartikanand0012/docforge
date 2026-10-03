"""Alembic environment. Migrations are hand-written, so there is no autogenerate metadata."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from docforge.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def run_migrations() -> None:
    # As the owner: the application's own login may not change the schema.
    url = config.attributes.get("url") or get_settings().migration_database_url.get_secret_value()
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline (SQL script) migrations are not supported")

run_migrations()
