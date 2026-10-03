"""`python -m docforge.evals.gate`: the ship gate over the eval reports.

CI replays every eval offline, then this checks the reports against the floors committed in
`evals/gate.json`. A prompt, model or code change that reads worse, misses a defect, leaks
across organisations or costs noticeably more fails the build. A floor that cannot be found
fails too: a renamed metric must not pass by disappearing.
"""

import argparse
import json
import operator
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
}


class GateError(ValueError):
    """The gate file itself is wrong."""


@dataclass(frozen=True)
class Check:
    report: str
    path: str
    op: str
    value: float
    why: str = ""


@dataclass(frozen=True)
class Result:
    report: str
    label: str
    passed: bool
    detail: str


def load_gate(path: Path) -> list[Check]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        checks = [Check(**item) for item in raw]
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise GateError(f"{path} is not a list of checks: {error}") from error
    for check in checks:
        if check.op not in _OPS:
            raise GateError(f"unknown comparison {check.op!r} for {check.report} {check.path}")
    return checks


def _lookup(report: Any, path: str) -> Any:
    node = report
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            raise KeyError(path)
        node = node[key]
    return node


def check_gate(reports: Path, checks: Sequence[Check]) -> list[Result]:
    loaded: dict[str, Any] = {}
    results = []
    for check in checks:
        label = f"{check.report} {check.path} {check.op} {check.value}"
        if check.report not in loaded:
            file = reports / f"{check.report}.json"
            loaded[check.report] = (
                json.loads(file.read_text(encoding="utf-8")) if file.exists() else None
            )
        report = loaded[check.report]
        if report is None:
            results.append(Result(check.report, label, False, "report missing"))
            continue
        try:
            actual = _lookup(report, check.path)
        except KeyError:
            results.append(Result(check.report, label, False, "metric missing"))
            continue
        passed = isinstance(actual, int | float) and _OPS[check.op](actual, check.value)
        results.append(Result(check.report, label, passed, f"is {actual}"))
    return results


def format_gate(results: Sequence[Result]) -> str:
    lines = [
        f"{'PASS' if result.passed else 'FAIL'}  {result.label}  ({result.detail})"
        for result in results
    ]
    failed = sum(not result.passed for result in results)
    lines.append(f"{len(results) - failed} of {len(results)} gate checks passed")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m docforge.evals.gate", description=__doc__)
    parser.add_argument("--reports", type=Path, default=Path("evals/baselines"))
    parser.add_argument("--gate", type=Path, default=Path("evals/gate.json"))
    args = parser.parse_args(argv)
    results = check_gate(args.reports, load_gate(args.gate))
    print(format_gate(results))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
