"""The certificate eval: values read, seeded out-of-limit results caught, clean ones left alone."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from docforge.evals.coa import format_coa_report, run_coa_eval

from docforge.extraction.coa import COA_SPEC
from docforge.extraction.pipeline import ExtractionPipeline
from fakes import MappedParser, ScriptedProvider, cited

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "coa"
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def subset(tmp_path: Path, cases: list[str]) -> Path:
    for case in cases:
        shutil.copytree(FIXTURES / case, tmp_path / case)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"seed": 1, "cases": cases}), encoding="utf-8"
    )
    return tmp_path


def perfect(
    directory: Path, build: RawFromLabel, edit: Callable[[str, dict[str, Any]], None] | None = None
) -> ExtractionPipeline[Any]:
    parser, replies = MappedParser(), []
    for case in sorted(p.name for p in directory.glob("coa_*")):
        label = json.loads((directory / case / "label.json").read_text(encoding="utf-8"))
        raw = build(label)
        if edit:
            edit(case, raw)
        parser.add((directory / case / "coa.pdf").read_bytes(), cited(label, "coa", raw))
        replies.append(json.dumps(raw))
    return ExtractionPipeline(parser, ScriptedProvider(replies), COA_SPEC)


def test_perfect_readings_score_every_value_and_catch_every_seeded_result(
    tmp_path: Path, raw_coa_from_label: RawFromLabel
) -> None:
    directory = subset(tmp_path, ["coa_001", "coa_002", "coa_005"])

    report = run_coa_eval(directory, perfect(directory, raw_coa_from_label))

    summary = report.summary
    assert summary.fields_correct == summary.fields_total > 0
    assert (summary.seeded, summary.seeded_caught) == (2, 2)
    assert summary.contradictions_expected == summary.contradictions_caught == 1
    assert (summary.clean, summary.clean_flagged) == (1, 0)


def test_a_misread_result_that_hides_a_defect_is_counted_as_missed(
    tmp_path: Path, raw_coa_from_label: RawFromLabel
) -> None:
    directory = subset(tmp_path, ["coa_002"])

    def hide(case: str, raw: dict[str, Any]) -> None:
        for test in raw["tests"]:
            if test["name"]["text"] == "Assay":
                test["result"]["text"] = "99.0 %"

    report = run_coa_eval(directory, perfect(directory, raw_coa_from_label, hide))

    assert (report.summary.seeded, report.summary.seeded_caught) == (1, 0)
    assert report.summary.fields_correct < report.summary.fields_total
    assert "coa_002" in format_coa_report(report)
