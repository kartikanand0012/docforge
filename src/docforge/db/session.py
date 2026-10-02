"""Engine and session factory."""

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session, sessionmaker

SessionFactory = sessionmaker[Session]


def make_engine(url: URL | str) -> Engine:
    # pre_ping: a worker may sit idle for a long time between jobs.
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> SessionFactory:
    # expire_on_commit=False: rows stay readable after the transaction that loaded them.
    return sessionmaker(engine, expire_on_commit=False)
