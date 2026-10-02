"""The Gemini provider against a stand-in client: request mapping, usage, retries."""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors
from pydantic import BaseModel

from docforge.llm.base import LLMError, LLMQuotaExhausted, LLMRequest
from docforge.llm.gemini import GeminiProvider


class Answer(BaseModel):
    value: str


REQUEST = LLMRequest(system="be exact", prompt="the document", schema=Answer, prompt_version="v1")


def reply(text: str | None = '{"value": "ok"}') -> SimpleNamespace:
    usage = SimpleNamespace(prompt_token_count=120, candidates_token_count=30)
    return SimpleNamespace(text=text, usage_metadata=usage)


def api_error(code: int) -> errors.APIError:
    return errors.APIError(code, {"error": {"message": f"status {code}", "status": "X"}})


class FakeModels:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def provider(
    outcomes: list[Any], sleeps: list[float] | None = None
) -> tuple[GeminiProvider, FakeModels]:
    models = FakeModels(outcomes)
    recorded = sleeps if sleeps is not None else []
    gemini = GeminiProvider(
        model="gemini-test",
        client=SimpleNamespace(models=models),
        sleep=recorded.append,
    )
    return gemini, models


def test_maps_the_request_onto_a_structured_output_call() -> None:
    gemini, models = provider([reply()])

    gemini.generate(REQUEST)

    (call,) = models.calls
    assert call["model"] == "gemini-test"
    assert call["contents"] == "the document"
    config = call["config"]
    assert config.system_instruction == "be exact"
    assert config.response_mime_type == "application/json"
    assert config.response_schema is Answer
    assert config.temperature == 0


def test_returns_text_usage_and_latency() -> None:
    gemini, _ = provider([reply()])

    response = gemini.generate(REQUEST)

    assert response.text == '{"value": "ok"}'
    assert (response.provider, response.model) == ("gemini", "gemini-test")
    assert (response.input_tokens, response.output_tokens) == (120, 30)
    assert response.latency_ms >= 0


@pytest.mark.parametrize("code", [429, 500, 503])
def test_retries_transient_errors_with_growing_delays(code: int) -> None:
    sleeps: list[float] = []
    gemini, models = provider([api_error(code), api_error(code), reply()], sleeps)

    response = gemini.generate(REQUEST)

    assert response.text == '{"value": "ok"}'
    assert len(models.calls) == 3
    assert len(sleeps) == 2
    assert sleeps[0] < sleeps[1]


def test_waits_as_long_as_the_api_asks_when_rate_limited() -> None:
    limited = errors.APIError(
        429,
        {
            "error": {
                "message": "quota",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}
                ],
            }
        },
    )
    sleeps: list[float] = []
    gemini, _ = provider([limited, reply()], sleeps)

    gemini.generate(REQUEST)

    assert sleeps == [7.0]


def test_an_exhausted_daily_quota_is_not_retried() -> None:
    # Seen on the free tier: retrying cannot succeed until the quota resets.
    exhausted = errors.APIError(
        429,
        {
            "error": {
                "message": "You exceeded your current quota",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "violations": [
                            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                        ]
                    },
                    {"retryDelay": "12s"},
                ],
            }
        },
    )
    sleeps: list[float] = []
    gemini, models = provider([exhausted, reply()], sleeps)

    with pytest.raises(LLMQuotaExhausted, match="daily quota"):
        gemini.generate(REQUEST)

    assert len(models.calls) == 1
    assert sleeps == []


def test_errors_carry_the_api_status_for_diagnosis() -> None:
    gemini, _ = provider([api_error(400)])

    with pytest.raises(LLMError, match="status 400 X: status 400"):
        gemini.generate(REQUEST)


def test_automatic_function_calling_is_off() -> None:
    gemini, models = provider([reply()])

    gemini.generate(REQUEST)

    assert models.calls[0]["config"].automatic_function_calling.disable is True


def test_gives_up_after_the_last_attempt() -> None:
    sleeps: list[float] = []
    gemini, models = provider([api_error(429)] * 4, sleeps)

    with pytest.raises(LLMError, match="429"):
        gemini.generate(REQUEST)

    assert len(models.calls) == 4
    assert len(sleeps) == 3


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_permanent_errors_are_not_retried(code: int) -> None:
    sleeps: list[float] = []
    gemini, models = provider([api_error(code), reply()], sleeps)

    with pytest.raises(LLMError, match=str(code)):
        gemini.generate(REQUEST)

    assert len(models.calls) == 1
    assert sleeps == []


def test_an_empty_reply_is_an_error() -> None:
    gemini, _ = provider([reply(text=None)])

    with pytest.raises(LLMError, match="empty"):
        gemini.generate(REQUEST)


def test_needs_a_key_or_a_client() -> None:
    with pytest.raises(ValueError, match="api_key"):
        GeminiProvider(model="gemini-test")


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("timed out"), httpx.ConnectError("no route to host")]
)
def test_network_failures_are_retried_then_reported_as_provider_errors(
    failure: Exception,
) -> None:
    sleeps: list[float] = []
    gemini, models = provider([failure] * 4, sleeps)

    with pytest.raises(LLMError, match="could not reach"):
        gemini.generate(REQUEST)

    assert len(models.calls) == 4
    assert len(sleeps) == 3


def test_output_is_capped() -> None:
    gemini, models = provider([reply()])

    gemini.generate(REQUEST)

    assert models.calls[0]["config"].max_output_tokens == 32768


def test_thinking_tokens_are_reported_separately() -> None:
    thoughtful = reply()
    thoughtful.usage_metadata.thoughts_token_count = 900
    gemini, _ = provider([thoughtful])

    response = gemini.generate(REQUEST)

    assert response.thinking_tokens == 900
    assert response.output_tokens == 30


def test_a_real_client_is_built_with_a_request_timeout() -> None:
    gemini = GeminiProvider(model="gemini-test", api_key="not-a-real-key", timeout_seconds=42)

    assert gemini.timeout_seconds == 42
