"""Anthropic's Claude as a provider, through its SDK, with native structured output.

Claude is held to the strict reply schema (`llm.schema`), so its reply is the JSON the
pipelines validate. The SDK's own retries are off: one retry policy applies, ours, as for
Gemini. A request always carries its timeout, so the SDK does not refuse a long one.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from docforge.llm.base import LLMError, LLMQuotaExhausted, LLMRequest, LLMResponse
from docforge.llm.retrying import MAX_DELAY, MAX_OUTPUT_TOKENS, RETRYABLE, backoff, retry_after
from docforge.llm.schema import strict_json_schema

logger = logging.getLogger(__name__)


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        *,
        client: Any = None,
        max_attempts: int = 4,
        base_delay: float = 2.0,
        timeout_seconds: float = 600.0,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        if client is None:
            if not api_key:
                raise ValueError("api_key is required when no client is given")
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._sleep = sleep

    def generate(self, request: LLMRequest) -> LLMResponse:
        import anthropic

        for attempt in range(1, self._max_attempts + 1):
            last = attempt == self._max_attempts
            started = time.perf_counter()
            try:
                message = self._client.messages.create(
                    model=self.model,
                    system=request.system,
                    messages=[{"role": "user", "content": request.prompt}],
                    temperature=0,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    output_config={
                        "format": {
                            "type": "json_schema",
                            "schema": strict_json_schema(request.schema),
                        }
                    },
                    timeout=self.timeout_seconds,
                )
            except anthropic.APIStatusError as error:
                summary = _summary(error)
                if _out_of_credit(error):
                    raise LLMQuotaExhausted(f"Anthropic credit is used up ({summary})") from error
                if error.status_code not in RETRYABLE or last:
                    raise LLMError(f"Anthropic request failed with {summary}") from error
                wait = retry_after(error.response.headers) or backoff(self._base_delay, attempt)
                self._sleep(min(wait, MAX_DELAY))
                continue
            except (anthropic.APITimeoutError, anthropic.APIConnectionError) as error:
                if last:
                    raise LLMError(f"could not reach Anthropic: {type(error).__name__}") from error
                self._sleep(backoff(self._base_delay, attempt))
                continue
            latency_ms = (time.perf_counter() - started) * 1000
            if message.stop_reason == "refusal":
                raise LLMError("the model declined to answer")
            if message.model != self.model:
                logger.warning("asked for %s, Anthropic served %s", self.model, message.model)
            text = "".join(block.text for block in message.content if block.type == "text")
            if not text:
                raise LLMError("Anthropic returned an empty reply")
            usage = message.usage
            return LLMResponse(
                text=text,
                provider=self.name,
                model=self.model,
                input_tokens=usage.input_tokens
                + (usage.cache_creation_input_tokens or 0)
                + (usage.cache_read_input_tokens or 0),
                output_tokens=usage.output_tokens,
                thinking_tokens=None,
                latency_ms=round(latency_ms, 1),
            )
        raise AssertionError("unreachable")  # pragma: no cover


def _summary(error: Any) -> str:
    """Status, error type and request id, for support: never the prompt."""
    body = error.body if isinstance(error.body, dict) else {}
    kind = (body.get("error") or {}).get("type", "error")
    request_id = error.response.headers.get("request-id", "unknown")
    return f"status {error.status_code} {kind} (request {request_id})"


def _out_of_credit(error: Any) -> bool:
    body = error.body if isinstance(error.body, dict) else {}
    details = body.get("error") or {}
    message = str(details.get("message", "")).lower()
    return details.get("type") == "billing_error" or "credit balance" in message
