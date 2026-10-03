"""The eval gate: the reports CI reproduces must hold the floors committed in evals/gate.json."""

import json
import shutil
from pathlib import Path

import pytest

from docforge.evals.gate import GateError, check_gate, format_gate, load_gate, main

BASELINES = Path("evals/baselines")
GATE = Path("evals/gate.json")


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    shutil.copytree(BASELINES, tmp_path / "baselines")
    return tmp_path / "baselines"


def worsen(directory: Path, name: str, path: list[str], value: object) -> None:
    file = directory / f"{name}.json"
    report = json.loads(file.read_text())
    node = report
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    file.write_text(json.dumps(report))


def test_the_committed_reports_hold_every_floor() -> None:
    results = check_gate(BASELINES, load_gate(GATE))

    assert results
    assert all(result.passed for result in results), format_gate(results)


def test_the_gate_covers_every_report_and_the_tenant_leak_count() -> None:
    gate = load_gate(GATE)
    covered = {check.report for check in gate}

    assert covered >= {"invoice", "multipage", "trust", "scans", "coa", "search"}
    assert any("cross_tenant_hits" in check.path for check in gate)
    assert any("input_tokens" in check.path for check in gate), "cost must be gated too"


@pytest.mark.parametrize(
    ("name", "path", "value", "named"),
    [
        ("invoice", ["summary", "fields", "accuracy"], 0.95, "invoice summary.fields.accuracy"),
        ("invoice", ["summary", "citations", "accuracy"], 0.90, "summary.citations.accuracy"),
        ("trust", ["summary", "seeded_cases_caught"], 8, "seeded_cases_caught"),
        ("coa", ["summary", "clean_flagged"], 2, "clean_flagged"),
        ("search", ["modes", "hybrid", "cross_tenant_hits"], 1, "cross_tenant_hits"),
        ("invoice", ["usage", "input_tokens"], 90_000, "usage.input_tokens"),
    ],
)
def test_a_worse_report_fails_and_names_the_metric(
    reports: Path, name: str, path: list[str], value: object, named: str
) -> None:
    worsen(reports, name, path, value)

    results = check_gate(reports, load_gate(GATE))
    failed = [r for r in results if not r.passed]

    assert failed
    assert any(named in r.label for r in failed)
    assert "FAIL" in format_gate(results)


def test_a_missing_report_or_metric_fails_rather_than_passing(reports: Path) -> None:
    (reports / "coa.json").unlink()
    worsen(reports, "search", ["modes"], {})

    failed = [r for r in check_gate(reports, load_gate(GATE)) if not r.passed]

    assert any(r.report == "coa" for r in failed)
    assert any(r.report == "search" for r in failed)


def test_a_gate_file_with_an_unknown_comparison_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "gate.json"
    bad.write_text(json.dumps([{"report": "coa", "path": "summary.cases", "op": "~", "value": 1}]))

    with pytest.raises(GateError):
        load_gate(bad)


def test_the_command_exits_non_zero_on_a_failure(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--reports", str(reports), "--gate", str(GATE)]) == 0
    worsen(reports, "invoice", ["summary", "fields", "accuracy"], 0.5)

    assert main(["--reports", str(reports), "--gate", str(GATE)]) == 1
    assert "FAIL" in capsys.readouterr().out
