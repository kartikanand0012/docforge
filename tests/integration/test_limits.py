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

    with (
        first.hold("chat:t:a", at_most=2, seconds=60),
        second.hold("chat:t:a", at_most=2, seconds=60),
        pytest.raises(LimitReached),
    ):
        with first.hold("chat:t:a", at_most=2, seconds=60):
            pass
    with second.hold("chat:t:a", at_most=2, seconds=60):  # released on leaving
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
