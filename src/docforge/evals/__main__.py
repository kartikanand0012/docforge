"""`python -m docforge.evals`: run the invoice eval and write the report.

Replay mode (default) is offline. Record mode calls the parser and the model for anything
not yet recorded, and can be re-run to resume after a quota limit.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from docforge.config import get_settings
from docforge.evals.run import format_report, record_pipeline, replay_pipeline, run_eval
from docforge.evals.scoring import DocumentScore
from docforge.llm.base import LLMError, LLMQuotaExhausted
from docforge.parsing.base import ParseError


def _progress(score: DocumentScore) -> None:
    correct = sum(field.outcome == "correct" for field in score.scored)
    note = f" ({score.error})" if score.error else ""
    print(f"{score.pair_id}: {correct}/{len(score.scored)} fields correct{note}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m docforge.evals", description=__doc__)
    parser.add_argument("--mode", choices=("replay", "record"), default="replay")
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/synthetic"))
    parser.add_argument("--recordings", type=Path, default=Path("tests/fixtures/recorded"))
    parser.add_argument("--out", type=Path, default=Path("evals/baselines/invoice.json"))
    parser.add_argument("--model", default=settings.gemini_model)
    args = parser.parse_args(argv)

    if args.mode == "record":
        if settings.gemini_api_key is None:
            parser.error("record mode needs GEMINI_API_KEY")
        pipeline = record_pipeline(
            args.recordings, args.model, settings.gemini_api_key.get_secret_value()
        )
    else:
        pipeline = replay_pipeline(args.recordings, args.model)

    try:
        report = run_eval(args.fixtures, pipeline, on_document=_progress)
    except LLMQuotaExhausted as error:
        print(f"Stopped: {error}", file=sys.stderr)
        print("Replies so far are recorded; run `make eval-record` again later.", file=sys.stderr)
        return 2
    except (ParseError, LLMError) as error:
        print(f"Eval failed: {error}", file=sys.stderr)
        if args.mode == "replay":
            print("Recordings are missing or stale; run `make eval-record`.", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(format_report(report))
    print(f"Report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
