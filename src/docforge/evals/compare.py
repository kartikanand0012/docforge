"""`python -m docforge.evals.compare`: providers side by side, on the same eval sets.

Each measure is shown with its 95% interval (Wilson), because 20 invoices and 104 questions
are small: 72 of 72 correct is evidence the rate is above about 95%, not proof it is 100%.
Cost is in US dollars from each model's price, since tokenisers differ. What a provider has
not been measured on says so.

    python -m docforge.evals.compare --prices '{"anthropic/claude-sonnet-5-5": [3, 15]}'
"""

import argparse
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from docforge.config import get_settings

Prices = dict[str, tuple[float, float]]
_Z = 1.96


def wilson(successes: int, total: int) -> tuple[float, float]:
    """The 95% Wilson interval of a rate; (0, 1) when nothing was measured."""
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    centre = p + _Z**2 / (2 * total)
    spread = _Z * math.sqrt(p * (1 - p) / total + _Z**2 / (4 * total**2))
    scale = 1 + _Z**2 / total
    return max(0.0, (centre - spread) / scale), min(1.0, (centre + spread) / scale)


def _rate(successes: int, total: int) -> str:
    low, high = wilson(successes, total)
    return (
        f"{successes}/{total} ({successes / total:.1%}; 95% {low:.1%}-{high:.1%})" if total else "-"
    )


def _rows(
    name: str, report: dict[str, Any], price: tuple[float, float] | None
) -> list[tuple[str, str]]:
    """The headline measures of one report, as (measure, value)."""
    rows: list[tuple[str, str]] = []
    if name == "invoice":
        fields = report["summary"]["fields"]
        rows += [
            ("invoice fields correct", _rate(fields["correct"], fields["total"])),
            ("invoice fields wrong", str(fields["wrong"])),
        ]
        usage = report.get("usage") or {}
        documents = report["summary"].get("documents", 0)
        if price and documents:
            cost = (usage.get("input_tokens", 0) * price[0] + (usage.get("output_tokens", 0)
                    + usage.get("thinking_tokens", 0)) * price[1]) / 1e6  # fmt: skip
            rows.append(("USD per invoice", f"{cost / documents:.5f}"))
    if name == "answers":
        answerable = report["answerable"]
        correct = round(report["answered_correctly"] * answerable)
        rows += [
            ("answered correctly", _rate(correct, answerable)),
            ("wrong answers", str(report["wrong_answers"])),
            ("answered the unanswerable", str(report["answered_unanswerable"])),
        ]
        if price:
            cost = (report["input_tokens_per_question"] * price[0]
                    + report["output_tokens_per_question"] * price[1]) / 1e6  # fmt: skip
            rows.append(("USD per question", f"{cost:.5f}"))
    return rows


_REPORTS = ("invoice", "answers")


def compare(reports: Path, prices: Prices) -> str:
    """A table of every provider with reports, Gemini's first."""
    gemini = json.loads((reports / "invoice.json").read_text(encoding="utf-8"))
    columns: dict[str, Path] = {f"gemini/{gemini['model']}": reports}
    for provider_dir in sorted(p for p in reports.iterdir() if p.is_dir()):
        for model_dir in sorted(m for m in provider_dir.iterdir() if m.is_dir()):
            columns[f"{provider_dir.name}/{model_dir.name}"] = model_dir
    values: dict[str, dict[str, str]] = {}
    measures: list[str] = []
    for column, directory in columns.items():
        values[column] = {}
        for name in _REPORTS:
            file = directory / f"{name}.json"
            if not file.exists():
                values[column][f"{name} report"] = "not measured"
                if f"{name} report" not in measures:
                    measures.append(f"{name} report")
                continue
            report = json.loads(file.read_text(encoding="utf-8"))
            for measure, value in _rows(name, report, prices.get(column)):
                values[column][measure] = value
                if measure not in measures:
                    measures.append(measure)
    lines = ["| Measure | " + " | ".join(columns) + " |", "|---" * (len(columns) + 1) + "|"]
    for measure in measures:
        cells = [values[column].get(measure, "-") for column in columns]
        lines.append(f"| {measure} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m docforge.evals.compare", description=__doc__)
    parser.add_argument("--reports", type=Path, default=Path("evals/baselines"))
    parser.add_argument(
        "--prices", help='JSON {"provider/model": [input, output]}; default MODEL_PRICES'
    )
    args = parser.parse_args(argv)
    prices = (
        {k: (float(v[0]), float(v[1])) for k, v in json.loads(args.prices).items()}
        if args.prices
        else get_settings().prices()
    )
    print(compare(args.reports, prices))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
