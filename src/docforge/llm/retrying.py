"""What Claude's and OpenAI's providers share: when to try again, and how long to wait."""

import math
import re
from collections.abc import Mapping

MAX_DELAY = 90.0
MAX_OUTPUT_TOKENS = 32768  # as for Gemini: a bound on cost and time
RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


def retry_after(headers: Mapping[str, str] | None) -> float | None:
    """The wait a server asked for, in seconds: its milliseconds if it gave them. A wait that
    is not a finite positive number (an HTTP date, "nan", "-5") is ignored: back off instead."""
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        value = (headers or {}).get(name)
        if value is None:
            continue
        try:
            seconds = float(value) / scale
        except ValueError:
            return None
        return seconds if math.isfinite(seconds) and seconds > 0 else None
    return None


def backoff(base: float, attempt: int) -> float:
    return float(base * 2 ** (attempt - 1))


def served_as(pinned: str, served: str) -> bool:
    """The pin itself, or a dated snapshot of it ("gpt-5-2026-08-07"), not another model
    whose name starts the same ("gpt-5-mini")."""
    return (
        served == pinned
        or re.fullmatch(re.escape(pinned) + r"-\d{4}-?\d{2}-?\d{2}", served) is not None
    )
