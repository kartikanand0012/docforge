"""The evals, run against another provider: the same sets, the same floors, reports kept
apart. Gemini's reports stay where they are; another provider's go under its name and model."""

from pathlib import Path

import pytest

from docforge.config import Settings
from docforge.evals.__main__ import main, report_path


@pytest.fixture(autouse=True)
def no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("docforge.evals.__main__.get_settings", lambda: Settings(_env_file=None))


def test_geminis_reports_stay_and_anothers_go_under_its_name_and_model() -> None:
    base = Path("evals/baselines")
    assert report_path(base, "gemini", "gemini-3.5-flash-lite", "invoice") == base / "invoice.json"
    assert report_path(base, "anthropic", "claude-sonnet-5-5", "invoice") == (
        base / "anthropic" / "claude-sonnet-5-5" / "invoice.json"
    )


def test_recording_with_a_provider_needs_its_key(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--provider", "anthropic", "--mode", "record"])
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_a_provider_without_recordings_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--provider", "anthropic", "--recordings", str(tmp_path), "--out",
                 str(tmp_path / "r.json")]) == 1  # fmt: skip
    assert "record" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("suite", ["search", "search-heldout", "mcp"])
def test_suites_without_a_model_call_refuse_a_provider(
    suite: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["--suite", suite, "--provider", "anthropic"])
    assert "no model" in capsys.readouterr().err


def test_a_moving_model_alias_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--provider", "anthropic", "--model", "claude-sonnet-latest"])
    assert "latest" in capsys.readouterr().err


def test_another_gemini_model_does_not_overwrite_the_committed_reports() -> None:
    base = Path("evals/baselines")
    assert report_path(base, "gemini", "gemini-x", "invoice", default="gemini-y") == (
        base / "gemini" / "gemini-x" / "invoice.json"
    )
    assert (
        report_path(base, "gemini", "gemini-y", "invoice", default="gemini-y")
        == base / "invoice.json"
    )
