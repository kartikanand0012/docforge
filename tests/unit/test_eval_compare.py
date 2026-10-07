"""Providers side by side: the same measures, with how sure each is, and what each costs."""

import json
import shutil
from pathlib import Path

from docforge.evals.compare import compare, wilson

BASELINES = Path("evals/baselines")


def test_a_rate_comes_with_its_95_percent_interval() -> None:
    low, high = wilson(72, 72)
    assert high == 1.0 and 0.94 < low < 0.96  # 72 of 72 is not proof of 100%
    assert wilson(0, 0) == (0.0, 1.0)


def test_each_provider_is_shown_beside_gemini_with_its_cost(tmp_path: Path) -> None:
    reports = tmp_path / "baselines"
    shutil.copytree(BASELINES, reports)
    own = reports / "anthropic" / "claude-x"
    own.mkdir(parents=True)
    answers = json.loads((BASELINES / "answers.json").read_text(encoding="utf-8"))
    answers |= {"provider": "anthropic", "model": "claude-x"}
    (own / "answers.json").write_text(json.dumps(answers), encoding="utf-8")

    table = compare(reports, prices={"anthropic/claude-x": (3.0, 15.0)})

    assert "gemini/gemini-3.5-flash-lite" in table and "anthropic/claude-x" in table
    assert "answered correctly" in table and "95%" in table
    assert "USD per question" in table
    assert "not measured" in table  # its other reports are missing, and say so
