"""The gate's proof: a shortened invoice prompt, run live once and recorded, is blocked.

The variant drops three rules from the prompt to save tokens (copy values exactly, cite the
blocks, mind misaligned headers). Its report is replayed offline from the recorded replies.
"""

import shutil
from pathlib import Path

from docforge.config import get_settings
from docforge.evals.demo import SHORTENED_PROMPT_VERSION, shortened_prompt_pipeline
from docforge.evals.gate import check_gate, load_gate
from docforge.evals.run import run_eval
from docforge.extraction.prompt import PROMPT_VERSION

RECORDINGS = Path("tests/fixtures/recorded")


def test_the_shortened_prompt_is_a_different_prompt_version() -> None:
    assert SHORTENED_PROMPT_VERSION != PROMPT_VERSION


def test_the_gate_blocks_the_shortened_prompt(tmp_path: Path) -> None:
    pipeline = shortened_prompt_pipeline(RECORDINGS, get_settings().gemini_model)
    report = run_eval(Path("tests/fixtures/synthetic"), pipeline)
    reports = tmp_path / "baselines"
    shutil.copytree("evals/baselines", reports)
    (reports / "invoice.json").write_text(report.model_dump_json())

    failed = [r for r in check_gate(reports, load_gate(Path("evals/gate.json"))) if not r.passed]

    assert report.prompt_version == SHORTENED_PROMPT_VERSION
    assert any(r.report == "invoice" for r in failed)
    assert all(r.report == "invoice" for r in failed)


def test_the_committed_demo_report_is_reproduced() -> None:
    pipeline = shortened_prompt_pipeline(RECORDINGS, get_settings().gemini_model)
    report = run_eval(Path("tests/fixtures/synthetic"), pipeline)

    committed = Path("evals/demos/shortened-prompt/invoice.json").read_text()
    assert report.model_dump_json(indent=2) + "\n" == committed
