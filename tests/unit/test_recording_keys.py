"""Recordings of several providers side by side, without moving Gemini's.

Every committed Gemini recording keeps its key: the gate replays them. Another provider's
replies are keyed by provider as well, so the same request recorded from Claude and from
OpenAI are two recordings, kept in a directory per provider.
"""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from docforge.llm.base import LLMError, LLMRequest, LLMResponse
from docforge.llm.replay import RecordingProvider


class Reply(BaseModel):
    answer: str


REQUEST = LLMRequest(
    system="Be exact.", prompt="What is 2 + 2?", schema=Reply, prompt_version="t-1"
)


def old_key(model: str, request: LLMRequest) -> str:
    """How a key was made before providers were part of it."""
    identity = {
        "model": model,
        "prompt_version": request.prompt_version,
        "system": request.system,
        "prompt": request.prompt,
        "schema": request.schema.model_json_schema(),
    }
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Live:
    def __init__(self, name: str, model: str) -> None:
        self.name, self.model = name, model

    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text='{"answer": "4"}', provider=self.name, model=self.model,
            input_tokens=1, output_tokens=1, latency_ms=1.0,
        )  # fmt: skip


def test_a_gemini_recording_keeps_the_key_it_always_had(tmp_path: Path) -> None:
    RecordingProvider(tmp_path, "gemini-x", Live("gemini", "gemini-x")).generate(REQUEST)
    (recorded,) = tmp_path.glob("*.json")
    assert recorded.name == f"{old_key('gemini-x', REQUEST)[:24]}.json"
    # And a replay-only recorder, which is Gemini's unless told otherwise, finds it.
    assert RecordingProvider(tmp_path, "gemini-x").generate(REQUEST).text == '{"answer": "4"}'


def test_another_providers_reply_is_keyed_by_provider_and_named_by_it(tmp_path: Path) -> None:
    claude = RecordingProvider(tmp_path, "m", Live("anthropic", "m"), provider="anthropic")
    claude.generate(REQUEST)
    (recorded,) = tmp_path.glob("*.json")
    assert recorded.name != f"{old_key('m', REQUEST)[:24]}.json"
    replay = RecordingProvider(tmp_path, "m", provider="anthropic")
    assert replay.name == "anthropic" and replay.generate(REQUEST).provider == "anthropic"
    with pytest.raises(LLMError):  # the same request, recorded for another provider: a miss
        RecordingProvider(tmp_path, "m", provider="openai").generate(REQUEST)


def test_a_live_provider_of_another_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="openai"):
        RecordingProvider(tmp_path, "m", Live("openai", "m"), provider="anthropic")
