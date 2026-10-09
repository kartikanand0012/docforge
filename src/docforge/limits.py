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

    def allow_hourly(self, key: str, per_hour: int) -> bool:
        """Count one request against `key` this hour; False once over `per_hour`."""
        ...


class DatabaseLimits:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def allow(self, key: str, per_minute: int) -> bool:
        return self._allow(key, per_minute, "minute")

    def allow_hourly(self, key: str, per_hour: int) -> bool:
        # Its own key: an hour's window starts at the same instant as one of its minutes.
        return self._allow(f"hour:{key}", per_hour, "hour")

    def _allow(self, key: str, limit: int, unit: str) -> bool:
        # `unit` is one of two fixed words, never input: it names the window.
        span = {"minute": 60, "hour": 3600}[unit]
        with self._sessions.begin() as session:
            count = session.execute(
                text(
                    "INSERT INTO rate_windows (key, window_start, count) "
                    "VALUES (:key, date_trunc(:unit, now()), 1) "
                    "ON CONFLICT (key, window_start) "
                    "DO UPDATE SET count = rate_windows.count + 1 RETURNING count"
                ),
                {"key": key, "unit": unit},
            ).scalar_one()
            # A sliding window: the one before counts for the part of it still within the
            # last minute (or hour), so a caller cannot double the rate at the turn of one.
            before = session.execute(
                text(
                    "SELECT coalesce(max(count), 0) * (1 - extract(epoch FROM now() - "
                    "date_trunc(:unit, now())) / :span) FROM rate_windows "
                    "WHERE key = :key AND window_start = date_trunc(:unit, now()) "
                    "- make_interval(secs => :span)"
                ),
                {"key": key, "unit": unit, "span": span},
            ).scalar_one()
            count = float(count) + float(before)
        if random.random() < 0.01:  # noqa: S311 - housekeeping, not security
            self.sweep()
        return count <= limit

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
        """Counts older than two hours (an hourly count needs the hour before) and places
        past their expiry, for every key."""
        with self._sessions.begin() as session:
            session.execute(
                text("DELETE FROM rate_windows WHERE window_start < now() - interval '2 hours'")
            )
            session.execute(text("DELETE FROM leases WHERE expires_at < now()"))


class LocalLimits:
    """In this process only: for tests and tools with no database."""

    def __init__(self) -> None:
        self._seen: dict[str, deque[float]] = {}
        self._held: Counter[str] = Counter()
        self._lock = threading.Lock()

    def allow(self, key: str, per_minute: int) -> bool:
        return self._allow(key, per_minute, 60)

    def allow_hourly(self, key: str, per_hour: int) -> bool:
        return self._allow(f"hour:{key}", per_hour, 3600)

    def _allow(self, key: str, limit: int, seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = self._seen.setdefault(key, deque())
            while recent and recent[0] <= now - seconds:
                recent.popleft()
            if len(recent) >= limit:
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
