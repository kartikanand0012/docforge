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
    value: float | str  # a string only with ==: a pinned model, prompt or dataset name
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
        if isinstance(check.value, str) and check.op != "==":
            raise GateError(f"{check.report} {check.path}: text can only be pinned with ==")
    return checks


def _comparable(actual: Any, wanted: float | str) -> bool:
    if isinstance(wanted, str):
        return isinstance(actual, str)
    # bool is an int to Python; a metric that turned into true/false is not a number.
    return isinstance(actual, int | float) and not isinstance(actual, bool)


def _lookup(report: Any, path: str) -> Any:
    node = report
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            raise KeyError(path)
        node = node[key]
    return node


def for_providers(checks: Sequence[Check], reports: Path) -> list[Check]:
    """`checks`, and for every other provider or model with reports under
    `<reports>/<provider>/<model>/`, every one of Gemini's checks on its reports: the same
    floors, its own provider and model pinned. Gemini's token ceilings are left out, since
    tokenisers differ (providers are compared in dollars). A report it lacks fails as
    missing: a provider cannot pass by being half measured."""
    out = list(checks)
    # Only reports a model made (they say whose): search and the MCP server call none.
    from_a_model = {
        check.report
        for check in checks
        if "/" not in check.report
        and "provider" in (_load_reports(reports, {check.report})[check.report] or {})
    }
    for provider_dir in sorted(p for p in reports.iterdir() if p.is_dir()):
        for model_dir in sorted(m for m in provider_dir.iterdir() if m.is_dir()):
            prefix = f"{provider_dir.name}/{model_dir.name}"
            for check in checks:
                if check.report not in from_a_model or "tokens" in check.path:
                    continue
                value = {"provider": provider_dir.name, "model": model_dir.name}.get(
                    check.path, check.value
                )
                out.append(
                    Check(f"{prefix}/{check.report}", check.path, check.op, value, check.why)
                )
    return out


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
        passed = _comparable(actual, check.value) and _OPS[check.op](actual, check.value)
        results.append(Result(check.report, label, passed, f"is {actual}"))
    return results


def _load_reports(directory: Path, names: set[str]) -> dict[str, Any]:
    out = {}
    for name in names:
        file = directory / f"{name}.json"
        out[name] = json.loads(file.read_text(encoding="utf-8")) if file.exists() else None
    return out


def compare_with_base(reports: Path, base: Path, checks: Sequence[Check]) -> list[Result]:
    """Each floor's metric must be no worse than on the base branch, not just above the floor:
    a change that loses ground passes floors set with room to spare. Metrics new on this
    branch, and pinned (==) values, are not compared."""
    ordered = [c for c in checks if c.op in (">=", "<=") and not isinstance(c.value, str)]
    names = {c.report for c in ordered}
    mine, theirs = _load_reports(reports, names), _load_reports(base, names)
    results = []
    for check in ordered:
        try:
            before = _lookup(theirs[check.report], check.path)
        except KeyError:
            continue  # new on this branch
        label = f"{check.report} {check.path} no worse than base {before}"
        try:
            now = _lookup(mine[check.report], check.path)
        except KeyError:
            results.append(Result(check.report, label, False, "metric missing"))
            continue
        passed = _comparable(now, 0.0) and _OPS[check.op](now, before)
        results.append(Result(check.report, label, passed, f"is {now}"))
    return results


def loosened(checks: Sequence[Check], base: Sequence[Check]) -> list[str]:
    """Floors this branch relaxes or removes compared with the base branch's gate file."""
    now = {(c.report, c.path): c for c in checks}
    found = []
    for old in base:
        new = now.get((old.report, old.path))
        name = f"{old.report} {old.path}"
        if new is None:
            found.append(f"{name}: removed")
        elif new.op != old.op or (
            not isinstance(old.value, str)
            and not isinstance(new.value, str)
            and (
                (old.op == ">=" and new.value < old.value)
                or (old.op == "<=" and new.value > old.value)
            )
        ):
            found.append(f"{name}: {old.op} {old.value} -> {new.op} {new.value}")
    return found


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
    parser.add_argument("--base-reports", type=Path, help="the base branch's evals/baselines")
    parser.add_argument("--base-gate", type=Path, help="the base branch's evals/gate.json")
    args = parser.parse_args(argv)
    checks = load_gate(args.gate)
    results = check_gate(args.reports, for_providers(checks, args.reports))
    if args.base_reports is not None:
        results += compare_with_base(args.reports, args.base_reports, checks)
    print(format_gate(results))
    relaxed = loosened(checks, load_gate(args.base_gate)) if args.base_gate else []
    for line in relaxed:
        print(f"LOOSENED  {line}")
    return 0 if all(result.passed for result in results) and not relaxed else 1


if __name__ == "__main__":
    raise SystemExit(main())
