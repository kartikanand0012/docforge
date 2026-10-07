"""Which provider a task is given, built from the settings: Gemini unless told otherwise.

Without its key, a deployment cannot extract (it says which key), while chat replays that
provider's recordings, as the demo and the browser test do. Each provider's recordings are
kept in a directory of their own.
"""

from pathlib import Path

import pytest

from docforge.config import Settings
from docforge.llm.anthropic import AnthropicProvider
from docforge.llm.gemini import GeminiProvider
from docforge.llm.openai import OpenAIProvider
from docforge.llm.replay import RecordingProvider
from docforge.wiring import build_provider, recorded_provider


def settings(**overrides: str) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CHAT_PROVIDER",
                 "EXTRACTION_PROVIDER", "OPENAI_MODEL", "OPENAI_REASONING_EFFORT"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)


def test_gemini_is_built_unless_told_otherwise() -> None:
    provider = build_provider(settings(GEMINI_API_KEY="g"), "extraction")
    assert isinstance(provider, GeminiProvider) and provider.model == "gemini-3.5-flash-lite"


def test_each_task_gets_its_own_provider_model_and_timeout() -> None:
    chosen = settings(
        CHAT_PROVIDER="anthropic", ANTHROPIC_API_KEY="a", EXTRACTION_PROVIDER="openai",
        OPENAI_API_KEY="o", OPENAI_MODEL="o-pinned-1", OPENAI_REASONING_EFFORT="low",
        LLM_TIMEOUT_SECONDS="300",
    )  # fmt: skip
    chat = build_provider(chosen, "chat")
    assert isinstance(chat, AnthropicProvider)
    assert (chat.model, chat.timeout_seconds) == ("claude-sonnet-5-5", 300.0)
    extraction = build_provider(chosen, "extraction")
    assert isinstance(extraction, OpenAIProvider)
    assert (extraction.model, extraction.reasoning_effort) == ("o-pinned-1", "low")


def test_without_its_key_a_provider_is_not_built_and_the_key_is_named() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        build_provider(settings(EXTRACTION_PROVIDER="anthropic"), "extraction")


def test_recordings_are_kept_per_provider(tmp_path: Path) -> None:
    gemini = recorded_provider(settings(RECORDINGS_DIR=str(tmp_path)), "chat")
    claude = recorded_provider(
        settings(RECORDINGS_DIR=str(tmp_path), CHAT_PROVIDER="anthropic"), "chat"
    )
    assert isinstance(gemini, RecordingProvider) and isinstance(claude, RecordingProvider)
    assert (gemini.name, gemini.directory) == ("gemini", tmp_path / "llm")
    assert (claude.name, claude.directory) == ("anthropic", tmp_path / "llm-anthropic")
    assert claude.model == "claude-sonnet-5-5"


def test_a_replay_deployment_never_calls_a_provider_even_with_a_key() -> None:
    from docforge.wiring import chat_provider

    replay = settings(GEMINI_API_KEY="g", PIPELINE_FACTORY="docforge.wiring:build_replay_pipelines")
    assert isinstance(chat_provider(replay), RecordingProvider)
    live = settings(GEMINI_API_KEY="g")
    assert isinstance(chat_provider(live), GeminiProvider)
    assert isinstance(chat_provider(settings()), RecordingProvider)  # no key: replay
