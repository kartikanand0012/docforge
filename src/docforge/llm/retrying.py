"""What Claude's and OpenAI's providers share: when to try again, and how long to wait."""

from collections.abc import Mapping

MAX_DELAY = 90.0
MAX_OUTPUT_TOKENS = 32768  # as for Gemini: a bound on cost and time
RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


def retry_after(headers: Mapping[str, str] | None) -> float | None:
    """The wait a server asked for, in seconds, if it said one."""
    value = (headers or {}).get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:  # an HTTP date: not used by these APIs; back off instead
        return None


def backoff(base: float, attempt: int) -> float:
    return float(base * 2 ** (attempt - 1))
