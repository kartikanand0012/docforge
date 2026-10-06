"""Claude and OpenAI as providers, through their real SDKs over a fake transport.

What is sent (the strict schema, no temperature where a model refuses it, OpenAI told not to
store), what comes back (the JSON reply, tokens with reasoning split out), and how failures
become DocForge's errors: rate limits and overloads retried, out of credit a quota stop, a bad
request or a refusal not retried.
"""

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from docforge.llm.anthropic import AnthropicProvider
from docforge.llm.base import LLMError, LLMQuotaExhausted, LLMRequest
from docforge.llm.openai import OpenAIProvider
from docforge.llm.schema import strict_json_schema


class Reply(BaseModel):
    total: str | None
    lines: list[str]


REQUEST = LLMRequest(system="Read it.", prompt="Invoice text", schema=Reply, prompt_version="t-1")
ANSWER = {"total": "98,697.00", "lines": ["a"]}


class Recorder:
    """Answers each request in turn and keeps what was sent."""

    def __init__(self, *replies: httpx.Response) -> None:
        self.replies = list(replies)
        self.sent: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        return self.replies.pop(0)


def transport(recorder: Recorder) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(recorder))


# --- Anthropic -------------------------------------------------------------------------------


def claude_message(text: str, *, stop: str = "end_turn", model: str = "claude-x") -> httpx.Response:
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 0,
        },
    }
    return httpx.Response(200, json=body, headers={"request-id": "req_1"})


def claude_error(status: int, kind: str, message: str = "nope", **headers: str) -> httpx.Response:
    body = {"type": "error", "error": {"type": kind, "message": message}}
    return httpx.Response(status, json=body, headers={"request-id": "req_9", **headers})


def claude(recorder: Recorder, sleeps: list[float] | None = None) -> AnthropicProvider:
    import anthropic

    client = anthropic.Anthropic(api_key="test", max_retries=0, http_client=transport(recorder))
    waits = sleeps if sleeps is not None else []
    return AnthropicProvider("claude-x", client=client, sleep=waits.append)


def test_claude_is_sent_the_strict_schema_and_its_reply_comes_back_as_json() -> None:
    recorder = Recorder(claude_message(json.dumps(ANSWER)))
    response = claude(recorder).generate(REQUEST)

    (sent,) = recorder.sent
    assert sent["model"] == "claude-x" and sent["system"] == "Read it."
    assert sent["temperature"] == 0 and "thinking" not in sent
    assert sent["messages"] == [{"role": "user", "content": "Invoice text"}]
    schema = strict_json_schema(Reply)
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": schema}
    assert json.loads(response.text) == ANSWER
    assert (response.provider, response.model) == ("anthropic", "claude-x")
    assert (response.input_tokens, response.output_tokens) == (105, 20)  # cache writes count
    assert response.thinking_tokens is None


def test_claude_rate_limits_and_overloads_are_retried_waiting_as_told() -> None:
    sleeps: list[float] = []
    recorder = Recorder(
        claude_error(429, "rate_limit_error", **{"retry-after": "7"}),
        claude_error(529, "overloaded_error"),
        claude_message(json.dumps(ANSWER)),
    )
    assert json.loads(claude(recorder, sleeps).generate(REQUEST).text) == ANSWER
    assert sleeps == [7.0, 4.0]  # the server's wait, then backoff (2 s, then 4 s)


def test_claude_out_of_credit_is_a_quota_stop_and_a_bad_request_is_not_retried() -> None:
    low = claude_error(400, "invalid_request_error", "Your credit balance is too low to use it")
    with pytest.raises(LLMQuotaExhausted, match="credit"):
        claude(Recorder(low)).generate(REQUEST)
    recorder = Recorder(claude_error(401, "authentication_error"))
    with pytest.raises(LLMError, match="401") as raised:
        claude(recorder).generate(REQUEST)
    assert "req_9" in str(raised.value) and "Invoice text" not in str(raised.value)
    assert len(recorder.sent) == 1


def test_claude_declining_is_an_error_and_a_cut_off_reply_is_returned_as_it_is() -> None:
    with pytest.raises(LLMError, match="declined"):
        claude(Recorder(claude_message("", stop="refusal"))).generate(REQUEST)
    cut = claude(Recorder(claude_message('{"total": "9', stop="max_tokens"))).generate(REQUEST)
    assert cut.text == '{"total": "9'  # the pipeline's validation decides


def test_claude_serving_another_model_than_the_pin_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    claude(Recorder(claude_message(json.dumps(ANSWER), model="claude-y"))).generate(REQUEST)
    assert "claude-y" in caplog.text


# --- OpenAI ----------------------------------------------------------------------------------


def openai_response(
    text: str,
    *,
    status: str = "completed",
    reasoning: int = 0,
    refusal: str | None = None,
) -> httpx.Response:
    content = (
        [{"type": "refusal", "refusal": refusal}]
        if refusal
        else [{"type": "output_text", "text": text, "annotations": []}]
    )
    body = {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": "o-x",
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "output": [
            {
                "type": "message",
                "id": "m1",
                "status": "completed",
                "role": "assistant",
                "content": content,
            }
        ],
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 40},
            "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": reasoning},
            "total_tokens": 130,
        },
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }
    return httpx.Response(200, json=body, headers={"x-request-id": "req_2"})


def openai_error(status: int, code: str) -> httpx.Response:
    body = {"error": {"type": code, "code": code, "message": "no", "param": None}}
    return httpx.Response(status, json=body, headers={"x-request-id": "req_8"})


def gpt(
    recorder: Recorder, *, effort: str | None = None, sleeps: list[float] | None = None
) -> OpenAIProvider:
    import openai

    client = openai.OpenAI(api_key="test", max_retries=0, http_client=transport(recorder))
    waits = sleeps if sleeps is not None else []
    return OpenAIProvider("o-x", client=client, reasoning_effort=effort, sleep=waits.append)


def test_openai_is_sent_a_strict_schema_and_told_not_to_store_the_response() -> None:
    recorder = Recorder(openai_response(json.dumps(ANSWER)))
    response = gpt(recorder).generate(REQUEST)

    (sent,) = recorder.sent
    assert sent["model"] == "o-x" and sent["instructions"] == "Read it."
    assert sent["input"] == "Invoice text"
    assert sent["store"] is False and sent["temperature"] == 0
    assert sent["text"]["format"] == {
        "type": "json_schema",
        "name": "Reply",
        "schema": strict_json_schema(Reply),
        "strict": True,
    }
    assert json.loads(response.text) == ANSWER and response.provider == "openai"


def test_openai_reasoning_is_thinking_not_output_and_a_reasoning_model_has_no_temperature() -> None:
    recorder = Recorder(openai_response(json.dumps(ANSWER), reasoning=12))
    response = gpt(recorder, effort="low").generate(REQUEST)

    (sent,) = recorder.sent
    assert "temperature" not in sent and sent["reasoning"] == {"effort": "low"}
    assert (response.input_tokens, response.output_tokens, response.thinking_tokens) == (
        100,
        18,
        12,
    )


def test_openai_rate_limits_are_retried_and_no_credit_is_a_quota_stop() -> None:
    sleeps: list[float] = []
    recorder = Recorder(
        openai_error(429, "rate_limit_exceeded"), openai_response(json.dumps(ANSWER))
    )
    assert json.loads(gpt(recorder, sleeps=sleeps).generate(REQUEST).text) == ANSWER
    assert sleeps == [2.0]
    with pytest.raises(LLMQuotaExhausted, match="insufficient_quota"):
        gpt(Recorder(openai_error(429, "insufficient_quota"))).generate(REQUEST)
    with pytest.raises(LLMError, match="req_8"):
        gpt(Recorder(openai_error(400, "invalid_request_error"))).generate(REQUEST)


def test_openai_refusing_is_an_error_and_a_cut_off_reply_is_returned_as_it_is() -> None:
    with pytest.raises(LLMError, match="declined"):
        gpt(Recorder(openai_response("", refusal="I can't help"))).generate(REQUEST)
    cut = gpt(Recorder(openai_response('{"total": "9', status="incomplete"))).generate(REQUEST)
    assert cut.text == '{"total": "9'
