"""Engine and session factory."""

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session, sessionmaker

from docforge.db import tenancy  # noqa: F401 - registers the per-transaction tenant setting

SessionFactory = sessionmaker[Session]


# A statement waits at most this long for a lock, and a transaction left open is ended, so a
# stuck process cannot hold others up indefinitely.
_SESSION_OPTIONS = "-c lock_timeout=15000 -c idle_in_transaction_session_timeout=120000"


def make_engine(url: URL | str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,  # a worker may sit idle for a long time between jobs
        # The audit chain relies on each statement seeing what was committed before it.
        isolation_level="READ COMMITTED",
        connect_args={"options": _SESSION_OPTIONS},
    )


def make_session_factory(engine: Engine) -> SessionFactory:
    # expire_on_commit=False: rows stay readable after the transaction that loaded them.
    return sessionmaker(engine, expire_on_commit=False)
