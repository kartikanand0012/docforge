"""Caps that hold across every API process, because they are kept in the database: requests
per minute, and things held at once (questions in flight, open streams)."""

import threading
import time

import pytest

from docforge.db.session import SessionFactory
from docforge.limits import DatabaseLimits, LimitReached

pytestmark = pytest.mark.integration


def test_requests_per_minute_are_counted_across_processes(sessions: SessionFactory) -> None:
    first, second = DatabaseLimits(sessions), DatabaseLimits(sessions)  # two API processes

    allowed = [
        limits.allow("search:t:a", per_minute=3) for limits in (first, second, first, second)
    ]

    assert allowed == [True, True, True, False]
    assert first.allow("search:t:b", per_minute=3)  # another caller has its own count


def test_what_is_held_at_once_is_capped_across_processes(sessions: SessionFactory) -> None:
    first, second = DatabaseLimits(sessions), DatabaseLimits(sessions)
    key = "chat:t:a"

    # Two places held, one in each process: a third is refused.
    with (
        first.hold(key, at_most=2, seconds=60),
        second.hold(key, at_most=2, seconds=60),
        pytest.raises(LimitReached),
        first.hold(key, at_most=2, seconds=60),
    ):
        pass
    with second.hold(key, at_most=2, seconds=60):  # given back on leaving
        pass


def test_a_hold_left_by_a_process_that_died_expires(sessions: SessionFactory) -> None:
    limits = DatabaseLimits(sessions)
    abandoned = limits.hold("stream:t:a", at_most=1, seconds=1)
    abandoned.__enter__()  # never left: as if the process died holding it
    with pytest.raises(LimitReached), limits.hold("stream:t:a", at_most=1, seconds=1):
        pass

    time.sleep(1.2)
    with limits.hold("stream:t:a", at_most=1, seconds=60):
        pass


def test_the_cap_holds_when_requests_arrive_together(sessions: SessionFactory) -> None:
    limits = DatabaseLimits(sessions)
    barrier = threading.Barrier(6)
    got: list[bool] = []
    release = threading.Event()

    def take() -> None:
        barrier.wait()
        try:
            with limits.hold("chat:t:race", at_most=2, seconds=60):
                got.append(True)
                release.wait(5)
        except LimitReached:
            got.append(False)

    threads = [threading.Thread(target=take) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(1)
    release.set()
    for t in threads:
        t.join()

    assert got.count(True) == 2 and got.count(False) == 4


def test_old_counts_and_expired_places_are_swept(sessions: SessionFactory) -> None:
    from sqlalchemy import text

    limits = DatabaseLimits(sessions)
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO rate_windows VALUES ('old', now() - interval '2 hours', 5); "
                "INSERT INTO leases (key, expires_at) VALUES ('gone', now() - interval '1 minute')"
            )
        )

    limits.sweep()

    with sessions() as session:
        assert (
            session.execute(
                text("SELECT count(*) FROM rate_windows WHERE key = 'old'")
            ).scalar_one()
            == 0
        )
        assert (
            session.execute(text("SELECT count(*) FROM leases WHERE key = 'gone'")).scalar_one()
            == 0
        )


def test_a_failed_release_does_not_hide_what_happened_inside(sessions: SessionFactory) -> None:
    limits = DatabaseLimits(sessions)

    class Broken(Exception):
        pass

    real = limits._release

    def failing(lease: object) -> None:
        raise RuntimeError("database gone")

    limits._release = failing  # type: ignore[method-assign]
    with pytest.raises(Broken), limits.hold("k:t:a", at_most=1, seconds=60):
        raise Broken
    limits._release = real  # type: ignore[method-assign]


def test_the_minute_before_still_counts_so_no_burst_at_the_turn_of_a_minute(
    sessions: SessionFactory,
) -> None:
    from sqlalchemy import text

    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO rate_windows VALUES "
                "('burst:t:a', date_trunc('minute', now()) - interval '1 minute', 10000)"
            )
        )

    assert not DatabaseLimits(sessions).allow("burst:t:a", per_minute=3)
