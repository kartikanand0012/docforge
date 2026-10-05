"""Deterministic rules: checks in code that a model's output cannot talk its way past."""

from collections.abc import Callable, Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

Severity = Literal["error", "warning"]
Outcome = Literal["passed", "failed", "not_evaluated"]


class RuleResult(BaseModel):
    """The result of one rule on one subject (the document, or one line of it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str
    version: int  # raised whenever the rule's logic changes, so old results stay interpretable
    severity: Severity
    outcome: Outcome  # not_evaluated: an input was missing, which is itself routed to review
    message: str
    paths: tuple[str, ...]  # the fields the result is about


# A rule looks at one extraction and yields a result per subject.
Rule = Callable[[object], Iterable[RuleResult]]


def run_rules[T](
    rules: Iterable[Callable[[T], Iterable[RuleResult]]], subject: T
) -> tuple[RuleResult, ...]:
    return tuple(result for rule in rules for result in rule(subject))
