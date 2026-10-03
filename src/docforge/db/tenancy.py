"""Which tenant the current work is for, told to Postgres at the start of each transaction.

Row-level security policies compare each row's `tenant_id` with the `docforge.tenant_id`
setting. Service methods run inside `tenant_scope(tenant_id)`; every transaction begun in that
scope sets it, local to the transaction, so a pooled connection never carries it further.
Outside a scope it is unset and the application's role sees no tenant rows at all.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, SessionTransaction

_CURRENT: ContextVar[uuid.UUID | None] = ContextVar("docforge_tenant", default=None)


@contextmanager
def tenant_scope(tenant_id: uuid.UUID) -> Iterator[None]:
    token = _CURRENT.set(tenant_id)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_tenant() -> uuid.UUID | None:
    return _CURRENT.get()


def scoped[F: Callable[..., Any]](method: F) -> F:
    """Run a method whose arguments include `tenant_id` inside that tenant's scope."""
    parameters = signature(method)

    @wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        tenant_id = parameters.bind(*args, **kwargs).arguments["tenant_id"]
        with tenant_scope(tenant_id):
            return method(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


@event.listens_for(Session, "after_begin")
def _set_tenant(session: Session, transaction: SessionTransaction, connection: Connection) -> None:
    tenant_id = _CURRENT.get()
    if tenant_id is not None:
        connection.execute(
            text("SELECT set_config('docforge.tenant_id', :tenant, true)"),
            {"tenant": str(tenant_id)},
        )
