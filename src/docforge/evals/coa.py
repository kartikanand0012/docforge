"""The certificate eval: are values read as printed, and are out-of-limit results caught?

Every printed value is compared with the label (as printed text, since a certificate's values
are mostly words and limits). For each seeded certificate the eval asks whether the rule
flagged the seeded test, and, where the certificate still claims compliance, whether the
contradiction was flagged too. For clean certificates it counts any result flagged wrongly.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from docforge.extraction.coa import CoaExtraction
from docforge.extraction.pipeline import ExtractionError, ExtractionPipeline
from docforge.trust.verify import extracted_fields


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CaseResult(_Model):
    case_id: str
    fields_total: int
    fields_correct: int
    wrong: tuple[tuple[str, str | None, str | None], ...]  # path, expected, read
    seeded: tuple[str, ...]
    flagged: tuple[str, ...]  # test names whose result was flagged out of limit
    contradiction_expected: bool
    contradiction_flagged: bool
    decision: str


class CoaSummary(_Model):
    cases: int
    fields_total: int
    fields_correct: int
    seeded: int
    seeded_caught: int
    contradictions_expected: int
    contradictions_caught: int
    clean: int
    clean_flagged: int


class CoaReport(_Model):
    model: str
    prompt_version: str
    summary: CoaSummary
    cases: tuple[CaseResult, ...]


def _case(directory: Path, pipeline: ExtractionPipeline[Any]) -> CaseResult:
    label: dict[str, Any] = json.loads((directory / "label.json").read_text(encoding="utf-8"))
    printed = {box["path"]: box["text"] for box in label["document"]["boxes"]}
    names = [test["name"] for test in label["coa"]["tests"]]
    seeded = tuple(label["out_of_limit"])
    expected_contradiction = bool(seeded) and "complies with" in label["coa"]["conclusion"]
    try:
        result = pipeline.run((directory / "coa.pdf").read_bytes())
    except ExtractionError:
        return CaseResult(
            case_id=directory.name, fields_total=len(printed), fields_correct=0,
            wrong=tuple((path, text, None) for path, text in printed.items()),
            seeded=seeded, flagged=(), contradiction_expected=expected_contradiction,
            contradiction_flagged=False, decision="failed",
        )  # fmt: skip
    extraction = result.extraction
    assert isinstance(extraction, CoaExtraction)  # noqa: S101
    read = {path: field.raw for path, field in extracted_fields(extraction)}
    wrong = tuple(
        (path, text, read.get(path)) for path, text in printed.items() if read.get(path) != text
    )
    failed = [r for r in result.assessment.rules if r.outcome == "failed"]
    flagged = tuple(
        names[index]
        for index in (
            _test_index(r.paths[0]) for r in failed if r.rule_id == "coa.result_within_limit"
        )
        if index < len(names)
    )
    return CaseResult(
        case_id=directory.name,
        fields_total=len(printed),
        fields_correct=len(printed) - len(wrong),
        wrong=wrong,
        seeded=seeded,
        flagged=flagged,
        contradiction_expected=expected_contradiction,
        contradiction_flagged=any(r.rule_id == "coa.conclusion_consistent" for r in failed),
        decision=result.assessment.decision,
    )


def _test_index(path: str) -> int:
    """`tests[3].result` -> 3."""
    return int(path.split("[", 1)[1].split("]", 1)[0])


def run_coa_eval(fixtures: Path, pipeline: ExtractionPipeline[Any]) -> CoaReport:
    manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
    cases = tuple(_case(fixtures / case, pipeline) for case in manifest["cases"])
    seeded = [c for c in cases if c.seeded]
    clean = [c for c in cases if not c.seeded]
    return CoaReport(
        model=pipeline.provider.model,
        prompt_version=pipeline.spec.prompt_version,
        summary=CoaSummary(
            cases=len(cases),
            fields_total=sum(c.fields_total for c in cases),
            fields_correct=sum(c.fields_correct for c in cases),
            seeded=len(seeded),
            seeded_caught=sum(set(c.seeded) <= set(c.flagged) for c in seeded),
            contradictions_expected=sum(c.contradiction_expected for c in cases),
            contradictions_caught=sum(
                c.contradiction_expected and c.contradiction_flagged for c in cases
            ),
            clean=len(clean),
            clean_flagged=sum(bool(c.flagged) for c in clean),
        ),
        cases=cases,
    )


def format_coa_report(report: CoaReport) -> str:
    s = report.summary
    lines = [
        f"model {report.model}, prompt {report.prompt_version}",
        f"values read as printed: {s.fields_correct} of {s.fields_total}",
        f"seeded out-of-limit results caught: {s.seeded_caught} of {s.seeded}",
        f"'complies' over a failed result caught: {s.contradictions_caught} of "
        f"{s.contradictions_expected}",
        f"clean certificates flagged wrongly: {s.clean_flagged} of {s.clean}",
    ]
    for case in report.cases:
        missed = set(case.seeded) - set(case.flagged)
        if missed:
            lines.append(f"  missed {case.case_id}: {', '.join(sorted(missed))}")
        for path, expected, read in case.wrong:
            lines.append(f"  {case.case_id} {path}: expected {expected!r}, read {read!r}")
    return "\n".join(lines)
