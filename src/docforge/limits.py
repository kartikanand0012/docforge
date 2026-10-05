"""Caps on what one caller may do: requests per minute, and things held at once.

`DatabaseLimits` keeps them in Postgres, so they hold however many API processes run.
`LocalLimits` keeps them in the process, for tests and tools with no database.
"""

import logging
import random
import threading
import time
from collections import Counter, deque
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

from sqlalchemy import text

from docforge.db.session import SessionFactory

logger = logging.getLogger(__name__)


class LimitReached(Exception):
    """The caller already holds as many as it may."""


class Limits(Protocol):
    def allow(self, key: str, per_minute: int) -> bool:
        """Count one request against `key` this minute; False once over `per_minute`."""
        ...

    def hold(self, key: str, at_most: int, seconds: int) -> AbstractContextManager[None]:
        """Hold one of at most `at_most` places under `key` while inside; raises
        `LimitReached` if none is free. A place held longer than `seconds` is given back."""
        ...


class DatabaseLimits:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def allow(self, key: str, per_minute: int) -> bool:
        with self._sessions.begin() as session:
            count = session.execute(
                text(
                    "INSERT INTO rate_windows (key, window_start, count) "
                    "VALUES (:key, date_trunc('minute', now()), 1) "
                    "ON CONFLICT (key, window_start) "
                    "DO UPDATE SET count = rate_windows.count + 1 RETURNING count"
                ),
                {"key": key},
            ).scalar_one()
        if random.random() < 0.01:  # noqa: S311 - housekeeping, not security
            self.sweep()
        return int(count) <= per_minute

    @contextmanager
    def hold(self, key: str, at_most: int, seconds: int) -> Iterator[None]:
        with self._sessions.begin() as session:
            # One taker at a time per key: two cannot both take the last place.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"lease:{key}"}
            )
            session.execute(
                text("DELETE FROM leases WHERE key = :key AND expires_at < now()"), {"key": key}
            )
            held = session.execute(
                text("SELECT count(*) FROM leases WHERE key = :key"), {"key": key}
            ).scalar_one()
            if held >= at_most:
                raise LimitReached(key)
            lease = session.execute(
                text(
                    "INSERT INTO leases (key, expires_at) "
                    "VALUES (:key, now() + make_interval(secs => :seconds)) RETURNING id"
                ),
                {"key": key, "seconds": seconds},
            ).scalar_one()
        try:
            yield
        finally:
            try:
                self._release(lease)
            except Exception:
                # The place frees itself when it expires; what happened inside is what counts.
                logger.warning("could not give back a place under %s", key, exc_info=True)

    def _release(self, lease: object) -> None:
        with self._sessions.begin() as session:
            session.execute(text("DELETE FROM leases WHERE id = :id"), {"id": lease})

    def sweep(self) -> None:
        """Counts older than an hour and places past their expiry, for every key."""
        with self._sessions.begin() as session:
            session.execute(
                text("DELETE FROM rate_windows WHERE window_start < now() - interval '1 hour'")
            )
            session.execute(text("DELETE FROM leases WHERE expires_at < now()"))


class LocalLimits:
    """In this process only: for tests and tools with no database."""

    def __init__(self) -> None:
        self._seen: dict[str, deque[float]] = {}
        self._held: Counter[str] = Counter()
        self._lock = threading.Lock()

    def allow(self, key: str, per_minute: int) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = self._seen.setdefault(key, deque())
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if len(recent) >= per_minute:
                return False
            recent.append(now)
            return True

    @contextmanager
    def hold(self, key: str, at_most: int, seconds: int) -> Iterator[None]:
        with self._lock:
            if self._held[key] >= at_most:
                raise LimitReached(key)
            self._held[key] += 1
        try:
            yield
        finally:
            with self._lock:
                self._held[key] -= 1
                if self._held[key] <= 0:
                    del self._held[key]


__all__ = ["DatabaseLimits", "LimitReached", "Limits", "LocalLimits"]
