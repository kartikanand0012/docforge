"""Combine the checks into one assessment: per field, and for the document as a whole.

The decision has two values. `review` means a person must look, and `reasons` says why.
`accept` means only this: every value was found in the source text it cites and no check
failed. It does not mean the document is genuine or its figures true; a forged but
self-consistent document passes. That takes an independent record to compare with, which
is what matching against a purchase order adds. There is no numeric confidence: a number
here would not be calibrated against anything yet.
"""

from collections.abc import Callable, Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from docforge.extraction.schema import Issue
from docforge.parsing.base import ParsedDocument
from docforge.trust.rules import RuleResult, run_rules
from docforge.trust.verify import Box, Status, verify_extraction

Decision = Literal["accept", "review"]
_UNVERIFIED = {
    "not_in_cited_blocks": "not found in the cited source text",
    "no_citation": "no source cited",
}


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FieldAssessment(_Model):
    path: str
    status: Status
    needs_review: bool
    reasons: tuple[str, ...]
    boxes: tuple[Box, ...]  # where on the page the value was read from
    found_in: tuple[str, ...]  # blocks that do contain the value, when the citation is wrong


class Assessment(_Model):
    decision: Decision
    reasons: tuple[str, ...]  # empty when accepted
    fields: tuple[FieldAssessment, ...]  # one per extracted value
    rules: tuple[RuleResult, ...]
    issues: tuple[Issue, ...]  # values the normaliser could not read


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def assess[E: BaseModel](
    extraction: E,
    parsed: ParsedDocument,
    rules: Sequence[Callable[[E], Iterable[RuleResult]]],
) -> Assessment:
    checks = verify_extraction(extraction, parsed)
    results = run_rules(rules, extraction)
    issues: tuple[Issue, ...] = tuple(getattr(extraction, "issues", ()))

    failed = [r for r in results if r.outcome == "failed" and r.severity == "error"]
    unevaluated = [r for r in results if r.outcome == "not_evaluated" and r.severity == "error"]
    failed_by_path: dict[str, list[str]] = {}
    for result in failed:
        for path in result.paths:
            failed_by_path.setdefault(path, []).append(f"failed {result.rule_id}")

    fields = []
    for check in checks:
        reasons = [_UNVERIFIED[check.status]] if check.status != "verified" else []
        reasons += failed_by_path.get(check.path, [])
        fields.append(
            FieldAssessment(
                path=check.path,
                status=check.status,
                needs_review=bool(reasons),
                reasons=tuple(dict.fromkeys(reasons)),
                boxes=check.boxes,
                found_in=check.found_in,
            )
        )

    unverified = sum(check.status != "verified" for check in checks)
    reasons_for_review = []
    if unverified:
        verb = "was" if unverified == 1 else "were"
        reasons_for_review.append(
            f"{_count(unverified, 'value', 'values')} {verb} not found in the source text "
            f"{'it cites' if unverified == 1 else 'they cite'}"
        )
    if failed:
        names = ", ".join(dict.fromkeys(result.rule_id for result in failed))
        reasons_for_review.append(f"{_count(len(failed), 'check', 'checks')} failed: {names}")
    if unevaluated:
        reasons_for_review.append(
            f"{_count(len(unevaluated), 'check', 'checks')} could not be evaluated"
        )
    if issues:
        reasons_for_review.append(f"{_count(len(issues), 'value', 'values')} could not be read")

    return Assessment(
        decision="review" if reasons_for_review else "accept",
        reasons=tuple(reasons_for_review),
        fields=tuple(fields),
        rules=results,
        issues=issues,
    )
