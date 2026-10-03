"""The gate's proof, on two edits to the invoice prompt recorded live once and replayed here.

A shortened prompt that measures as good as the original passes; a prompt that "tidies"
values for a downstream system reads worse and is blocked.
"""

import shutil
from pathlib import Path

import pytest

from docforge.config import get_settings
from docforge.evals.demo import VARIANTS, variant_pipeline
from docforge.evals.gate import Result, check_gate, load_gate
from docforge.evals.run import EvalReport, run_eval
from docforge.extraction.prompt import PROMPT_VERSION

RECORDINGS = Path("tests/fixtures/recorded")


def replay(name: str) -> EvalReport:
    pipeline = variant_pipeline(name, RECORDINGS, get_settings().gemini_model)
    return run_eval(Path("tests/fixtures/synthetic"), pipeline)


def gate_with(report: EvalReport, tmp_path: Path) -> list[Result]:
    reports = tmp_path / "baselines"
    shutil.copytree("evals/baselines", reports)
    (reports / "invoice.json").write_text(report.model_dump_json())
    return [r for r in check_gate(reports, load_gate(Path("evals/gate.json"))) if not r.passed]


def test_each_variant_is_its_own_prompt_version() -> None:
    versions = [version for version, _ in VARIANTS.values()]
    assert PROMPT_VERSION not in versions
    assert len(set(versions)) == len(versions)


def test_the_gate_lets_a_shortened_prompt_that_measures_as_well_through(tmp_path: Path) -> None:
    assert gate_with(replay("shortened"), tmp_path) == []


def test_the_gate_blocks_a_prompt_that_tidies_values(tmp_path: Path) -> None:
    report = replay("normalising")
    failed = gate_with(report, tmp_path)

    assert {r.label.split(" >=")[0] for r in failed} >= {
        "invoice summary.fields.accuracy",
        "invoice summary.documents_fully_correct",
    }
    assert all(r.report == "invoice" for r in failed)
    # The trust layer turned unmatchable dates into missing values, not wrong ones.
    assert report.summary.fields.wrong == 0


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_the_committed_demo_reports_are_reproduced(name: str) -> None:
    committed = Path(f"evals/demos/{name}/invoice.json").read_text()
    assert replay(name).model_dump_json(indent=2) + "\n" == committed
