from pathlib import Path

import pytest
from pydantic import BaseModel

from docforge.llm.base import LLMError, LLMRequest, LLMResponse
from docforge.llm.replay import RecordingProvider


class Answer(BaseModel):
    value: str


class CountingProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            text=f'{{"value": "call {self.calls}"}}',
            provider=self.name,
            model=self.model,
            input_tokens=10,
            output_tokens=5,
            latency_ms=12.5,
        )


def request(prompt: str = "hello", prompt_version: str = "v1") -> LLMRequest:
    return LLMRequest(system="sys", prompt=prompt, schema=Answer, prompt_version=prompt_version)


def test_record_mode_calls_the_provider_once_and_then_replays(tmp_path: Path) -> None:
    inner = CountingProvider()
    provider = RecordingProvider(tmp_path, model="fake-1", inner=inner)

    first = provider.generate(request())
    second = provider.generate(request())

    assert inner.calls == 1
    assert first == second
    assert first.text == '{"value": "call 1"}'
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_replay_only_mode_serves_a_recording_made_earlier(tmp_path: Path) -> None:
    RecordingProvider(tmp_path, model="fake-1", inner=CountingProvider()).generate(request())

    replayed = RecordingProvider(tmp_path, model="fake-1").generate(request())

    assert replayed.text == '{"value": "call 1"}'
    assert replayed.input_tokens == 10
    assert replayed.model == "fake-1"


def test_replay_only_mode_fails_on_a_miss_instead_of_calling_out(tmp_path: Path) -> None:
    with pytest.raises(LLMError, match="no recorded response"):
        RecordingProvider(tmp_path, model="fake-1").generate(request())


@pytest.mark.parametrize(
    "changed",
    [
        LLMRequest(system="sys", prompt="different", schema=Answer, prompt_version="v1"),
        LLMRequest(system="other", prompt="hello", schema=Answer, prompt_version="v1"),
        LLMRequest(system="sys", prompt="hello", schema=Answer, prompt_version="v2"),
        LLMRequest(system="sys", prompt="hello", schema=LLMResponse, prompt_version="v1"),
    ],
)
def test_any_change_to_the_request_is_a_different_recording(
    tmp_path: Path, changed: LLMRequest
) -> None:
    inner = CountingProvider()
    provider = RecordingProvider(tmp_path, model="fake-1", inner=inner)

    provider.generate(request())
    provider.generate(changed)

    assert inner.calls == 2


def test_a_different_model_does_not_reuse_recordings(tmp_path: Path) -> None:
    RecordingProvider(tmp_path, model="fake-1", inner=CountingProvider()).generate(request())

    with pytest.raises(LLMError, match="no recorded response"):
        RecordingProvider(tmp_path, model="fake-2").generate(request())


def test_inner_provider_must_match_the_recorded_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="model"):
        RecordingProvider(tmp_path, model="fake-2", inner=CountingProvider())


def test_recordings_do_not_store_the_prompt(tmp_path: Path) -> None:
    provider = RecordingProvider(tmp_path, model="fake-1", inner=CountingProvider())

    provider.generate(request(prompt="a very distinctive prompt body"))

    (recording,) = tmp_path.glob("*.json")
    assert "a very distinctive prompt body" not in recording.read_text(encoding="utf-8")


def test_a_truncated_recording_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    inner = CountingProvider()
    provider = RecordingProvider(tmp_path, model="fake-1", inner=inner)
    provider.generate(request())
    (recording,) = tmp_path.glob("*.json")
    recording.write_text('{"key": "trunc', encoding="utf-8")

    provider.generate(request())

    assert inner.calls == 2
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_truncated_recording_in_replay_only_mode_is_reported_as_missing(
    tmp_path: Path,
) -> None:
    RecordingProvider(tmp_path, model="fake-1", inner=CountingProvider()).generate(request())
    (recording,) = tmp_path.glob("*.json")
    recording.write_text("{", encoding="utf-8")

    with pytest.raises(LLMError, match="no recorded response"):
        RecordingProvider(tmp_path, model="fake-1").generate(request())


def test_a_miss_in_replay_only_mode_says_it_was_not_recorded(tmp_path: Path) -> None:
    from docforge.llm.replay import NotRecorded

    with pytest.raises(NotRecorded):
        RecordingProvider(tmp_path, model="fake-1").generate(request())
    assert issubclass(NotRecorded, LLMError)  # anything that handled a model error still does
