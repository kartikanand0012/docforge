"""Gemini provider using the API's native structured output."""

import re
import time
from collections.abc import Callable
from typing import Any

import httpx
from google.genai import errors, types

from docforge.llm.base import LLMError, LLMQuotaExhausted, LLMRequest, LLMResponse

_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_DELAY = 90.0
MAX_OUTPUT_TOKENS = 32768  # a bound on cost and time; a 20-page invoice needs far less
_SECONDS = re.compile(r"(\d+(?:\.\d+)?)s")


def _suggested_delay(details: object) -> float | None:
    """The `retryDelay` the API sends with a rate-limit error, e.g. "34s"."""
    if isinstance(details, dict):
        delay = details.get("retryDelay")
        if isinstance(delay, str) and (match := _SECONDS.fullmatch(delay)):
            return float(match[1])
        details = list(details.values())
    if isinstance(details, list):
        for item in details:
            if (found := _suggested_delay(item)) is not None:
                return found
    return None


def _daily_quota_exhausted(details: object) -> bool:
    """True when a 429 names a per-day quota, which a short wait cannot clear."""
    if isinstance(details, dict):
        if "PerDay" in str(details.get("quotaId", "")):
            return True
        details = list(details.values())
    return isinstance(details, list) and any(_daily_quota_exhausted(item) for item in details)


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        *,
        client: Any = None,
        max_attempts: int = 4,
        base_delay: float = 2.0,
        timeout_seconds: float = 120.0,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        if client is None:
            if not api_key:
                raise ValueError("api_key is required when no client is given")
            from google import genai

            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
            )
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._sleep = sleep

    def generate(self, request: LLMRequest) -> LLMResponse:
        config = types.GenerateContentConfig(
            system_instruction=request.system,
            temperature=0,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
            response_schema=request.schema,
            # No tools are offered, so the SDK's function-calling loop has nothing to do.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        for attempt in range(1, self._max_attempts + 1):
            last = attempt == self._max_attempts
            started = time.perf_counter()
            try:
                reply = self._client.models.generate_content(
                    model=self.model, contents=request.prompt, config=config
                )
            except errors.APIError as error:
                summary = f"status {error.code} {error.status}: {error.message}"
                if error.code == 429 and _daily_quota_exhausted(error.details):
                    raise LLMQuotaExhausted(
                        f"Gemini daily quota for {self.model} is used up ({summary})"
                    ) from error
                if error.code not in _RETRYABLE or last:
                    raise LLMError(f"Gemini request failed with {summary}") from error
                # Honour the server's retry delay; otherwise back off exponentially.
                delay = _suggested_delay(error.details) or self._backoff(attempt)
                self._sleep(min(delay, _MAX_DELAY))
                continue
            except httpx.TransportError as error:  # timeouts and connection failures
                if last:
                    raise LLMError(f"could not reach Gemini: {type(error).__name__}") from error
                self._sleep(self._backoff(attempt))
                continue
            latency_ms = (time.perf_counter() - started) * 1000
            if not reply.text:
                raise LLMError("Gemini returned an empty reply")
            usage = reply.usage_metadata
            return LLMResponse(
                text=reply.text,
                provider=self.name,
                model=self.model,
                input_tokens=getattr(usage, "prompt_token_count", None),
                output_tokens=getattr(usage, "candidates_token_count", None),
                thinking_tokens=getattr(usage, "thoughts_token_count", None),
                latency_ms=round(latency_ms, 1),
            )
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int) -> float:
        return float(self._base_delay * 2 ** (attempt - 1))
