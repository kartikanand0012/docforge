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

    assert covered >= {"invoice", "multipage", "trust", "scans", "coa", "search", "search_heldout"}
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


def test_a_boolean_is_not_a_number_to_the_gate(reports: Path) -> None:
    worsen(reports, "coa", ["summary", "clean_flagged"], False)

    failed = [r for r in check_gate(reports, load_gate(GATE)) if not r.passed]

    assert any("clean_flagged" in r.label for r in failed)


def test_the_dataset_model_and_prompt_are_pinned(reports: Path) -> None:
    worsen(reports, "invoice", ["summary", "documents"], 10)
    worsen(reports, "invoice", ["prompt_version"], "invoice-v2")

    failed = {r.label.split(" ")[1] for r in check_gate(reports, load_gate(GATE)) if not r.passed}

    assert {"summary.documents", "prompt_version"} <= failed


def test_a_metric_worse_than_the_base_branch_fails_even_above_its_floor(reports: Path) -> None:
    from docforge.evals.gate import compare_with_base

    base = reports.parent / "base"
    shutil.copytree(reports, base)
    worsen(base, "invoice", ["summary", "citations", "accuracy"], 0.999)

    worse = compare_with_base(reports, base, load_gate(GATE))

    assert [r.label for r in worse if not r.passed] == [
        "invoice summary.citations.accuracy no worse than base 0.999"
    ]
    assert all(r.passed for r in compare_with_base(reports, reports, load_gate(GATE)))


def test_a_loosened_floor_is_named(tmp_path: Path) -> None:
    from docforge.evals.gate import loosened

    base = json.loads(GATE.read_text())
    changed = json.loads(GATE.read_text())
    for check in changed:
        if check["path"] == "summary.fields.accuracy" and check["report"] == "invoice":
            check["value"] = 0.9
    (tmp_path / "base.json").write_text(json.dumps(base))
    (tmp_path / "new.json").write_text(json.dumps(changed))

    found = loosened(load_gate(tmp_path / "new.json"), load_gate(tmp_path / "base.json"))

    assert found == ["invoice summary.fields.accuracy: >= 0.99 -> >= 0.9"]


def test_the_held_out_hard_slice_and_field_classes_are_gated() -> None:
    paths = {(c.report, c.path) for c in load_gate(GATE)}

    assert ("search_heldout", "modes.hybrid.by_kind.ocr_code.recall_at_5") in paths
    assert ("invoice", "summary.by_class.date.accuracy") in paths
    assert ("invoice", "summary.fields.wrong") in paths


# --- other providers (C15) ---------------------------------------------------------------------

MODEL_REPORTS = ("invoice", "multipage", "trust", "scans", "coa", "answers")


def test_every_report_from_a_model_pins_its_provider_model_and_prompts() -> None:
    pinned = {(c.report, c.path) for c in load_gate(GATE) if c.op == "=="}
    for report in MODEL_REPORTS:
        assert (report, "provider") in pinned, report
        assert (report, "model") in pinned, report
        assert any(r == report and "prompt_version" in p for r, p in pinned), report


def test_a_provider_with_any_report_is_held_to_every_floor_gemini_is(tmp_path: Path) -> None:
    """Reports under evals/baselines/<provider>/<model>/ get every Gemini check, with that
    provider and model pinned and Gemini's token ceilings left out (tokenisers differ). A
    missing report fails: a provider cannot pass by being half measured."""
    from docforge.evals.gate import for_providers

    reports = tmp_path / "baselines"
    shutil.copytree(BASELINES, reports)
    own = reports / "anthropic" / "claude-x"
    own.mkdir(parents=True)
    invoice = json.loads((BASELINES / "invoice.json").read_text(encoding="utf-8"))
    invoice |= {"provider": "anthropic", "model": "claude-x"}
    (own / "invoice.json").write_text(json.dumps(invoice), encoding="utf-8")

    checks = for_providers(load_gate(GATE), reports)
    results = [r for r in check_gate(reports, checks) if r.report.startswith("anthropic/")]

    invoice_results = [r for r in results if r.report == "anthropic/claude-x/invoice"]
    assert invoice_results and all(r.passed for r in invoice_results)
    assert not any("tokens" in r.label for r in results)
    missing = {r.report for r in results if r.detail == "report missing"}
    assert missing == {f"anthropic/claude-x/{name}" for name in MODEL_REPORTS if name != "invoice"}
    assert main(["--reports", str(reports)]) == 1


@pytest.mark.parametrize(
    "name", ["EvalReport", "TrustReport", "ScanReport", "CoaReport", "AnswerReport"]
)
def test_every_report_from_a_model_says_which_provider(name: str) -> None:
    import docforge.evals.answers as answers
    import docforge.evals.coa as coa
    import docforge.evals.run as run
    import docforge.evals.scans as scans
    import docforge.evals.trust as trust

    found = next(getattr(m, name) for m in (run, trust, scans, coa, answers) if hasattr(m, name))
    assert "provider" in found.model_fields
