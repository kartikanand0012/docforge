"""How long to wait when a provider asks: its milliseconds first, and nothing absurd."""

import pytest

from docforge.llm.retrying import retry_after


def test_milliseconds_are_preferred_and_seconds_are_read() -> None:
    assert retry_after({"retry-after-ms": "1500", "retry-after": "9"}) == 1.5
    assert retry_after({"retry-after": "7"}) == 7.0
    assert retry_after({}) is None and retry_after(None) is None


@pytest.mark.parametrize(
    "value", ["nan", "-5", "inf", "-inf", "soon", "Wed, 21 Oct 2026 07:28:00 GMT"]
)
def test_a_wait_that_is_not_a_finite_positive_number_is_ignored(value: str) -> None:
    assert retry_after({"retry-after": value}) is None
    assert retry_after({"retry-after-ms": value}) is None
