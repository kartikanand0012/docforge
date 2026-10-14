"""Accuracy on real invoices, scored against readings a person approved in DocForge.

No permissive set of real Indian GST invoices exists, so the truth is made with the product
itself: the owner uploads real bills, checks and corrects every field in review, and approves.
`pull` writes the newest approval of each invoice as a label (`<filename>.json`) beside the PDFs;
`run_real_eval` reads each PDF again and scores every printed field against its label.

Everything stays under `data/` (ignored by git): labels and the full report hold the bills'
values. `shareable_summary` is the part that may be committed or published - counts only.
"""

import json
import os
from collections.abc import Iterable, Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from docforge.evals.scoring import FIELD_CLASSES
from docforge.extraction.pipeline import ExtractionError, ExtractionPipeline
from docforge.extraction.schema import InvoiceExtraction
from docforge.llm.base import LLMError
from docforge.parsing.base import ParseError

RealOutcome = Literal["correct", "wrong", "missing", "invented"]
_EXPORT = "/v1/exports/documents.json"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RealField(_Model):
    path: str
    field_class: str
    expected: str | None
    actual: str | None
    outcome: RealOutcome


class RealDocument(_Model):
    name: str
    expected_lines: int
    extracted_lines: int
    extra_lines: int
    error: str | None
    fields: tuple[RealField, ...]


class RealTally(_Model):
    total: int
    correct: int
    wrong: int
    missing: int
    invented: int
    accuracy: float


class RealSummary(_Model):
    documents: int
    documents_fully_correct: int
    failed: int
    lines_expected: int
    lines_extracted: int
    extra_lines: int
    fields: RealTally
    by_class: dict[str, RealTally]
    by_field: dict[str, RealTally]


class RealReport(_Model):
    documents: tuple[RealDocument, ...]
    unlabelled: tuple[str, ...]  # PDFs with no approved reading yet
    without_pdf: tuple[str, ...]  # approved readings whose PDF is not in the folder
    summary: RealSummary


# --- scoring --------------------------------------------------------------------------------


def _values(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """(path, value) for every extracted field of a record, in the record's order."""
    if isinstance(node, dict) and {"value", "raw", "block_ids"} <= node.keys():
        yield path, node["value"]
    elif isinstance(node, dict):
        for key, child in node.items():
            if key not in ("issues", "schema_version"):
                yield from _values(child, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _values(child, f"{path}[{index}]")


def _leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1].split("[", 1)[0]


def _line(path: str) -> int | None:
    if not path.startswith("lines["):
        return None
    return int(path[len("lines[") : path.index("]")])


def _same(expected: Any, actual: Any) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        try:
            return Decimal(expected) == Decimal(actual)  # "12.0" and "12.00" are one amount
        except InvalidOperation:
            return expected.strip() == actual.strip()
    return bool(expected == actual)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def score_against_record(
    name: str,
    truth: dict[str, Any],
    actual: InvoiceExtraction | None,
    error: str | None = None,
) -> RealDocument:
    """Every printed field of `truth` (an approved record) against a fresh reading.

    A field empty in both is not counted. Lines are matched by position; a line the reading has
    beyond the record's is an extra line, not a list of invented fields.
    """
    expected = {path: value for path, value in _values(truth) if _leaf(path) in FIELD_CLASSES}
    got: dict[str, Any] = {}
    if actual is not None:
        got = {
            path: value
            for path, value in _values(actual.model_dump(mode="json"))
            if _leaf(path) in FIELD_CLASSES
        }
    expected_lines = len(truth.get("lines") or [])
    extracted_lines = len(actual.lines) if actual is not None else 0
    fields: list[RealField] = []
    for path in [*expected, *(path for path in got if path not in expected)]:
        line = _line(path)
        if line is not None and line >= expected_lines:
            continue  # an extra line: counted once below
        want, have = expected.get(path), got.get(path)
        if want is None and have is None:
            continue
        if want is None:
            outcome: RealOutcome = "invented"
        elif have is None:
            outcome = "missing"
        else:
            outcome = "correct" if _same(want, have) else "wrong"
        fields.append(
            RealField(
                path=path,
                field_class=FIELD_CLASSES[_leaf(path)],
                expected=_text(want),
                actual=_text(have),
                outcome=outcome,
            )
        )
    return RealDocument(
        name=name,
        expected_lines=expected_lines,
        extracted_lines=extracted_lines,
        extra_lines=max(0, extracted_lines - expected_lines),
        error=error,
        fields=tuple(fields),
    )


def _tally(fields: Iterable[RealField]) -> RealTally:
    outcomes = [field.outcome for field in fields]
    total = len(outcomes)
    correct = outcomes.count("correct")
    return RealTally(
        total=total,
        correct=correct,
        wrong=outcomes.count("wrong"),
        missing=outcomes.count("missing"),
        invented=outcomes.count("invented"),
        accuracy=round(correct / total, 4) if total else 0.0,
    )


def summarize(documents: Iterable[RealDocument]) -> RealSummary:
    documents = tuple(documents)
    fields = [field for document in documents for field in document.fields]
    classes = sorted({field.field_class for field in fields})
    leaves = sorted({_leaf(field.path) for field in fields})
    return RealSummary(
        documents=len(documents),
        documents_fully_correct=sum(
            document.error is None
            and document.extra_lines == 0
            and all(field.outcome == "correct" for field in document.fields)
            for document in documents
        ),
        failed=sum(document.error is not None for document in documents),
        lines_expected=sum(document.expected_lines for document in documents),
        lines_extracted=sum(document.extracted_lines for document in documents),
        extra_lines=sum(document.extra_lines for document in documents),
        fields=_tally(fields),
        by_class={name: _tally(f for f in fields if f.field_class == name) for name in classes},
        by_field={name: _tally(f for f in fields if _leaf(f.path) == name) for name in leaves},
    )


def shareable_summary(report: RealReport) -> dict[str, Any]:
    """The counts of a report, with no filename and no value from any bill."""
    return {
        **report.summary.model_dump(mode="json"),
        "unlabelled": len(report.unlabelled),
        "without_pdf": len(report.without_pdf),
    }


# --- labels ---------------------------------------------------------------------------------


def labels_from_export(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Labels by filename from the signed-records export (newest first).

    A document's newest signature decides: an approval is its label, a rejection means none.
    Two approved documents with one filename are refused, as a PDF could not be told apart.
    """
    decided: set[str] = set()
    labels: dict[str, dict[str, Any]] = {}
    for row in rows:
        document_id = row["document_id"]
        if document_id in decided:
            continue
        decided.add(document_id)
        if row.get("doc_type") != "invoice" or row.get("outcome") != "approved":
            continue
        filename = row["filename"]
        if filename in labels:
            raise ValueError(f"two approved invoices are named {filename!r}; rename one")
        labels[filename] = {
            "document_id": document_id,
            "filename": filename,
            "signed_at": row["signed_at"],
            "record": row["record"],
        }
    return labels


def _safe_name(filename: str) -> str:
    name = Path(filename).name
    if name != filename or name.startswith(".") or "\\" in name or not name:
        raise ValueError(f"refused a label filename that is not a plain file name: {filename!r}")
    return name


def pull(api_url: str, out_dir: Path, *, transport: httpx.BaseTransport | None = None) -> int:
    """Write a label for each approved invoice in the key's workspace; returns how many.

    The key is read from DOCFORGE_API_KEY (never an argument, so it stays out of shell
    history) and is sent only over https, or http to this machine.
    """
    parts = urlsplit(api_url)
    local = parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS
    if parts.scheme != "https" and not local:
        raise ValueError("the API must be reached over https (http only for localhost)")
    key = os.environ.get("DOCFORGE_API_KEY")
    if not key:
        raise ValueError("set DOCFORGE_API_KEY to an API key of the workspace holding the labels")
    with httpx.Client(
        base_url=api_url,
        transport=transport,
        timeout=60,
        headers={"Authorization": f"Bearer {key}"},
    ) as client:
        response = client.get(_EXPORT)
        response.raise_for_status()
    labels = labels_from_export(response.json())
    named = {_safe_name(filename): label for filename, label in labels.items()}
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, label in named.items():
        (out_dir / f"{name}.json").write_text(json.dumps(label, indent=2) + "\n", encoding="utf-8")
    return len(named)


# --- running --------------------------------------------------------------------------------


def run_real_eval(
    pdfs: Path, labels: Path, pipeline: ExtractionPipeline[InvoiceExtraction]
) -> RealReport:
    """Read every PDF that has a label and score it; name the PDFs and labels left unmatched."""
    by_name = {
        path.name.removesuffix(".json"): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(labels.glob("*.json"))
    }
    present = {path.name for path in pdfs.glob("*.pdf")}
    documents: list[RealDocument] = []
    for name in sorted(present & by_name.keys()):
        truth = by_name[name]["record"]
        try:
            extraction = pipeline.run((pdfs / name).read_bytes()).extraction
        except (ParseError, LLMError, ExtractionError) as error:
            documents.append(score_against_record(name, truth, None, error=str(error)))
            continue
        documents.append(score_against_record(name, truth, extraction))
    return RealReport(
        documents=tuple(documents),
        unlabelled=tuple(sorted(present - by_name.keys())),
        without_pdf=tuple(sorted(by_name.keys() - present)),
        summary=summarize(documents),
    )


def format_summary(report: RealReport) -> str:
    summary = report.summary
    lines = [
        f"documents: {summary.documents}, fully correct: {summary.documents_fully_correct}, "
        f"failed: {summary.failed}",
        f"lines: {summary.lines_extracted} read of {summary.lines_expected}, "
        f"{summary.extra_lines} extra",
        f"fields: {summary.fields.accuracy:.2%} of {summary.fields.total} "
        f"({summary.fields.wrong} wrong, {summary.fields.missing} missing, "
        f"{summary.fields.invented} invented)",
    ]
    for heading, tallies in (("by kind", summary.by_class), ("by field", summary.by_field)):
        lines.append(heading)
        lines += [
            f"  {name:<20} {tally.accuracy:7.2%}  {tally.correct}/{tally.total}"
            for name, tally in tallies.items()
        ]
    if report.unlabelled:
        lines.append(f"PDFs without an approved reading: {len(report.unlabelled)}")
    if report.without_pdf:
        lines.append(f"approved readings without their PDF: {len(report.without_pdf)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from docforge.config import get_settings
    from docforge.evals.run import record_pipeline, replay_pipeline

    parser = argparse.ArgumentParser(
        prog="python -m docforge.evals.real", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--root", type=Path, default=Path("data/real"))
    commands = parser.add_subparsers(dest="command", required=True)
    pulling = commands.add_parser("pull", help="labels from the approved readings (key in env)")
    pulling.add_argument("--api", required=True, help="e.g. https://<api host>")
    running = commands.add_parser("run", help="read every labelled PDF and score it")
    running.add_argument("--mode", choices=("replay", "record"), default="record")
    args = parser.parse_args(argv)
    root: Path = args.root

    if args.command == "pull":
        print(f"labels written: {pull(args.api, root / 'labels')}")
        return 0
    settings = get_settings()
    recordings = root / "recorded"
    if args.mode == "record":
        if settings.gemini_api_key is None:
            parser.error("recording needs GEMINI_API_KEY")
        key = settings.gemini_api_key.get_secret_value()
        pipeline = record_pipeline(recordings, settings.gemini_model, key)
    else:
        pipeline = replay_pipeline(recordings, settings.gemini_model)
    report = run_real_eval(root / "pdfs", root / "labels", pipeline)
    (root / "report.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (root / "summary.json").write_text(
        json.dumps(shareable_summary(report), indent=2) + "\n", encoding="utf-8"
    )
    print(format_summary(report))
    print(f"full report (holds the bills' values, keep private): {root / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
