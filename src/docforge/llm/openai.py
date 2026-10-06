"""OpenAI as a provider, through its SDK's Responses API, with strict structured output.

OpenAI is told not to keep the response (`store: false`). A reasoning model is sent its
effort and no temperature, which it refuses; its reasoning tokens are reported as thinking,
not output, so their cost is counted once. The SDK's own retries are off: ours apply.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from docforge.llm.base import LLMError, LLMQuotaExhausted, LLMRequest, LLMResponse
from docforge.llm.retrying import (
    MAX_DELAY,
    MAX_OUTPUT_TOKENS,
    RETRYABLE,
    backoff,
    retry_after,
    served_as,
)
from docforge.llm.schema import strict_json_schema

logger = logging.getLogger(__name__)


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        *,
        client: Any = None,
        reasoning_effort: str | None = None,
        max_attempts: int = 4,
        base_delay: float = 2.0,
        timeout_seconds: float = 600.0,
        sleep: Callable[[float], object] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if client is None:
            if not api_key:
                raise ValueError("api_key is required when no client is given")
            import openai

            client = openai.OpenAI(api_key=api_key, max_retries=0)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._sleep = sleep
        self._clock = clock

    def generate(self, request: LLMRequest) -> LLMResponse:
        import openai

        options: dict[str, Any] = (
            {"reasoning": {"effort": self.reasoning_effort}}
            if self.reasoning_effort
            else {"temperature": 0}
        )
        began = self._clock()
        for attempt in range(1, self._max_attempts + 1):
            last = attempt == self._max_attempts
            started = time.perf_counter()
            try:
                response = self._client.responses.create(
                    model=self.model,
                    instructions=request.system,
                    input=request.prompt,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                    store=False,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": request.schema.__name__,
                            "schema": strict_json_schema(request.schema),
                            "strict": True,
                        }
                    },
                    timeout=self.timeout_seconds,
                    **options,
                )
            except openai.APIStatusError as error:
                summary = _summary(error)
                if _code(error) == "insufficient_quota":
                    raise LLMQuotaExhausted(f"OpenAI credit is used up ({summary})") from error
                if error.status_code not in RETRYABLE or last:
                    raise LLMError(f"OpenAI request failed with {summary}") from error
                wait = retry_after(error.response.headers) or backoff(self._base_delay, attempt)
                self._wait(began, min(wait, MAX_DELAY), summary)
                continue
            except (openai.APITimeoutError, openai.APIConnectionError) as error:
                if last:
                    raise LLMError(f"could not reach OpenAI: {type(error).__name__}") from error
                self._wait(began, backoff(self._base_delay, attempt), type(error).__name__)
                continue
            latency_ms = (time.perf_counter() - started) * 1000
            parts = [
                part for item in response.output if item.type == "message" for part in item.content
            ]
            if any(part.type == "refusal" for part in parts):
                raise LLMError("the model declined to answer")
            if not served_as(self.model, response.model):
                logger.warning("asked for %s, OpenAI served %s", self.model, response.model)
            text = "".join(part.text for part in parts if part.type == "output_text")
            if not text:
                raise LLMError("OpenAI returned an empty reply")
            usage = response.usage
            reasoning = 0
            if usage is not None and usage.output_tokens_details is not None:
                reasoning = usage.output_tokens_details.reasoning_tokens or 0
            return LLMResponse(
                text=text,
                provider=self.name,
                model=self.model,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=(usage.output_tokens - reasoning) if usage else None,
                thinking_tokens=reasoning or None,
                latency_ms=round(latency_ms, 1),
                served_model=response.model,
            )
        raise AssertionError("unreachable")  # pragma: no cover

    def _wait(self, began: float, seconds: float, why: str) -> None:
        """Wait before the next attempt, unless that would run past the call's own time."""
        if self._clock() - began + seconds >= self.timeout_seconds:
            raise LLMError(
                f"OpenAI gave up after {self.timeout_seconds:.0f} s, the call's time ({why})"
            )
        self._sleep(seconds)


def _code(error: Any) -> str:
    body = error.body if isinstance(error.body, dict) else {}
    details = body.get("error", body) if isinstance(body.get("error", body), dict) else body
    return str(details.get("code") or details.get("type") or "")


def _summary(error: Any) -> str:
    """Status, error code and request id, for support: never the prompt."""
    request_id = error.response.headers.get("x-request-id", "unknown")
    return f"status {error.status_code} {_code(error) or 'error'} (request {request_id})"
