"""`python -m docforge.evals`: run the invoice eval and write the report.

Replay mode (default) is offline. Record mode calls the parser and the model for anything
not yet recorded, and can be re-run to resume after a quota limit.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from docforge.config import get_settings
from docforge.evals.answers import (
    AnswerResult,
    Question,
    format_answer_report,
    run_answer_eval,
)
from docforge.evals.coa import format_coa_report, run_coa_eval
from docforge.evals.run import format_report, record_pipeline, replay_pipeline, run_eval
from docforge.evals.scans import format_scan_report, run_scan_eval
from docforge.evals.scoring import DocumentScore
from docforge.evals.search import format_search_report, run_search_eval
from docforge.evals.search_heldout import format_heldout_report, run_heldout_eval
from docforge.evals.trust import format_trust_report, run_trust_eval, trust_pipelines
from docforge.extraction.coa import COA_SPEC
from docforge.extraction.pipeline import ExtractionPipeline, InvoicePipeline
from docforge.llm.base import LLMError, LLMQuotaExhausted
from docforge.llm.gemini import GeminiProvider
from docforge.llm.replay import RecordingProvider
from docforge.parsing.base import ParseError
from docforge.parsing.cache import CachingParser
from docforge.parsing.docling_parser import DoclingParser
from docforge.search.embeddings import EmbeddingMissing, GeminiEmbedder, RecordingEmbedder


def _progress(score: DocumentScore) -> None:
    correct = sum(field.outcome == "correct" for field in score.scored)
    note = f" ({score.error})" if score.error else ""
    print(f"{score.pair_id}: {correct}/{len(score.scored)} fields correct{note}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m docforge.evals", description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("extraction", "trust", "scans", "coa", "search", "search-heldout", "answers"),
        default="extraction",
    )
    parser.add_argument("--mode", choices=("replay", "record"), default="replay")
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/synthetic"))
    parser.add_argument("--recordings", type=Path, default=Path("tests/fixtures/recorded"))
    parser.add_argument("--seeded", type=Path, default=Path("tests/fixtures/seeded"))
    parser.add_argument("--scanned", type=Path, default=Path("tests/fixtures/scanned"))
    parser.add_argument("--coa", type=Path, default=Path("tests/fixtures/coa"))
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--model", default=settings.gemini_model)
    args = parser.parse_args(argv)

    names = {
        "extraction": "invoice",
        "trust": "trust",
        "scans": "scans",
        "coa": "coa",
        "search": "search",
        "search-heldout": "search_heldout",
        "answers": "answers",
    }
    out = args.out or Path(f"evals/baselines/{names[args.suite]}.json")
    if args.mode == "record" and settings.gemini_api_key is None:
        parser.error("record mode needs GEMINI_API_KEY")
    if args.suite == "answers":
        secret = settings.gemini_api_key if args.mode == "record" else None
        return _answers(args, out, settings.embedding_model, secret)
    if args.suite in ("search", "search-heldout"):
        secret = settings.gemini_api_key if args.mode == "record" else None
        return _search(args, out, settings.embedding_model, secret)
    if args.suite == "coa":
        secret = settings.gemini_api_key if args.mode == "record" else None
        return _coa(args, out, secret.get_secret_value() if secret is not None else None)
    if args.suite == "trust":
        secret = settings.gemini_api_key if args.mode == "record" else None
        key = secret.get_secret_value() if secret is not None else None
        return _trust(args, out, key)
    if args.mode == "record":
        assert settings.gemini_api_key is not None  # noqa: S101 - checked above
        pipeline = record_pipeline(
            args.recordings, args.model, settings.gemini_api_key.get_secret_value()
        )
    else:
        pipeline = replay_pipeline(args.recordings, args.model)
    if args.suite == "scans":
        return _scans(args, out, pipeline)

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

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(format_report(report))
    print(f"Report written to {out}")
    return 0


def _scans(args: argparse.Namespace, out: Path, pipeline: InvoicePipeline) -> int:
    variants = {"clean": args.fixtures}
    variants.update({path.name: path for path in sorted(args.scanned.iterdir()) if path.is_dir()})

    def progress(variant: str, score: DocumentScore) -> None:
        correct = sum(field.outcome == "correct" for field in score.scored)
        print(f"{variant} {score.pair_id}: {correct}/{len(score.scored)} correct", flush=True)

    try:
        # Orders are read from recordings made by the trust eval; they are never scanned.
        _, order_pipeline = trust_pipelines(args.recordings, args.model)
        report = run_scan_eval(
            variants, pipeline, on_document=progress, orders=(args.fixtures, order_pipeline)
        )
    except LLMQuotaExhausted as error:
        print(f"Stopped: {error}", file=sys.stderr)
        print("Replies so far are recorded; run `make eval-record` again later.", file=sys.stderr)
        return 2
    except (ParseError, LLMError) as error:
        print(f"Eval failed: {error}", file=sys.stderr)
        if args.mode == "replay":
            print("Recordings are missing or stale; run `make eval-record`.", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(format_scan_report(report))
    print(f"Report written to {out}")
    return 0


def _search(args: argparse.Namespace, out: Path, model: str, secret: Any) -> int:
    live = GeminiEmbedder(model, secret.get_secret_value()) if secret is not None else None
    embedder = RecordingEmbedder(args.recordings / "embeddings", live, model=model)
    try:
        if args.suite == "search-heldout":
            heldout = run_heldout_eval(args.fixtures, args.coa, args.recordings, embedder)
            body, summary = heldout.model_dump_json(indent=2), format_heldout_report(heldout)
        else:
            report = run_search_eval(args.fixtures, args.coa, args.recordings, embedder)
            body, summary = report.model_dump_json(indent=2), format_search_report(report)
    except (EmbeddingMissing, ParseError, LLMError) as error:
        print(f"Eval failed: {error}", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body + "\n", encoding="utf-8")
    print(summary)
    print(f"Report written to {out}")
    return 0


def _answers(args: argparse.Namespace, out: Path, embedding_model: str, secret: Any) -> int:
    key = secret.get_secret_value() if secret is not None else None
    embedder = RecordingEmbedder(
        args.recordings / "embeddings",
        GeminiEmbedder(embedding_model, key) if key else None,
        model=embedding_model,
    )
    provider = RecordingProvider(
        args.recordings / "llm", args.model, GeminiProvider(args.model, key) if key else None
    )

    def progress(question: Question, result: AnswerResult) -> None:
        print(f"{question.id}: {result.status}: {result.text[:90]}", flush=True)

    try:
        report = run_answer_eval(
            args.fixtures, args.coa, args.recordings, embedder, provider, on_answer=progress
        )
    except LLMQuotaExhausted as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 2
    except (EmbeddingMissing, ParseError, LLMError) as error:
        print(f"Eval failed: {error}", file=sys.stderr)
        if args.mode == "replay":
            print("Recordings are missing or stale; record the answer eval.", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(format_answer_report(report))
    print(f"Report written to {out}")
    return 0


def _coa(args: argparse.Namespace, out: Path, api_key: str | None) -> int:
    live = api_key is not None
    parser = CachingParser(args.recordings / "parsed", DoclingParser() if live else None)
    provider = RecordingProvider(
        args.recordings / "llm", args.model, GeminiProvider(args.model, api_key) if live else None
    )
    try:
        report = run_coa_eval(args.coa, ExtractionPipeline(parser, provider, COA_SPEC))
    except LLMQuotaExhausted as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 2
    except (ParseError, LLMError) as error:
        print(f"Eval failed: {error}", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(format_coa_report(report))
    print(f"Report written to {out}")
    return 0


def _trust(args: argparse.Namespace, out: Path, api_key: str | None) -> int:
    invoices, orders = trust_pipelines(args.recordings, args.model, api_key)
    try:
        report = run_trust_eval(args.fixtures, args.seeded, invoices, orders)
    except LLMQuotaExhausted as error:
        print(f"Stopped: {error}", file=sys.stderr)
        print("Replies so far are recorded; run `make eval-record` again later.", file=sys.stderr)
        return 2
    except (ParseError, LLMError) as error:
        print(f"Eval failed: {error}", file=sys.stderr)
        if api_key is None:
            print("Recordings are missing or stale; run `make eval-record`.", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(format_trust_report(report))
    print(f"Report written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
