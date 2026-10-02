"""Scoring an extraction against a label. The scorer decides what the baseline number means."""

import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docforge.evals.scoring import DocumentScore, score_invoice, summarize
from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.schema import RawInvoice
from docforge.parsing.base import BBox, Block, Page, ParsedDocument
from docforge.synth.models import PairLabel

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
_SEGMENT = re.compile(r"(\w+)|\[(\d+)\]")


def load(pair_id: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURES / pair_id / "label.json").read_text(encoding="utf-8")
    )
    return loaded


def raw_field(raw: dict[str, Any], path: str) -> dict[str, Any] | None:
    """The raw field at a label path such as `lines[0].qty`, or None if the schema has none."""
    node: Any = raw
    for name, index in _SEGMENT.findall(path):
        try:
            node = node[name] if name else node[int(index)]
        except (KeyError, IndexError):
            return None
    return node if isinstance(node, dict) else None


class Perfect:
    """A reply that copies every printed value and cites a block placed exactly on it."""

    def __init__(self, pair_id: str, build: RawFromLabel) -> None:
        label_json = load(pair_id)
        self.label = PairLabel.model_validate(label_json)
        boxes = self.label.documents["invoice"].boxes
        self.parsed = ParsedDocument(
            parser="fake",
            parser_version="0",
            pages=(Page(number=1, width=842, height=842),),
            blocks=tuple(
                Block(
                    id=f"b{n}",
                    kind="text",
                    text=box.text,
                    page=1,
                    bbox=BBox(x0=box.x0, y0=box.y0, x1=box.x1, y1=box.y1),
                )
                for n, box in enumerate(boxes, start=1)
            ),
        )
        self.block_of = {box.path: f"b{n}" for n, box in enumerate(boxes, start=1)}
        self.raw = build(label_json)
        for path, block_id in self.block_of.items():
            field = raw_field(self.raw, path)
            if field is not None:
                field["block_ids"] = [block_id]

    def score(self, raw: dict[str, Any] | None = None) -> DocumentScore:
        reply = RawInvoice.model_validate(raw if raw is not None else self.raw)
        return score_invoice(self.label, normalize_invoice(reply, self.parsed), self.parsed)

    def changed(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)


@pytest.fixture(scope="module")
def intra(raw_invoice_from_label: RawFromLabel) -> Perfect:
    return Perfect("pair_001", raw_invoice_from_label)


@pytest.fixture(scope="module")
def inter(raw_invoice_from_label: RawFromLabel) -> Perfect:
    return Perfect("pair_003", raw_invoice_from_label)


def outcome(score: DocumentScore, path: str) -> str:
    return next(field.outcome for field in score.fields if field.path == path)


def test_a_perfect_extraction_scores_every_printed_field_correct(intra: Perfect) -> None:
    score = intra.score()

    assert {field.outcome for field in score.scored} == {"correct"}
    assert all(field.cited for field in score.scored)
    assert score.fully_correct
    assert (score.expected_lines, score.extracted_lines) == (10, 10)


def test_only_printed_business_fields_are_scored(intra: Perfect) -> None:
    printed = {box.path for box in intra.label.documents["invoice"].boxes}
    serial_numbers = {path for path in printed if path.endswith(".sl_no")}

    scored = {field.path for field in intra.score().scored}

    assert scored == (printed - serial_numbers) | {"place_of_supply_code"}
    assert "supply_type" not in scored
    assert "lines[0].cgst" not in scored


def test_fields_are_grouped_into_classes(intra: Perfect) -> None:
    classes = {field.path: field.field_class for field in intra.score().fields}

    assert classes["invoice_no"] == "identifier"
    assert classes["seller.drug_licence_nos[1]"] == "identifier"
    assert classes["lines[0].batch_no"] == "identifier"
    assert classes["invoice_date"] == "date"
    assert classes["lines[0].expiry"] == "date"
    assert classes["lines[0].ptr"] == "amount"
    assert classes["totals.grand_total"] == "amount"
    assert classes["lines[0].free_qty"] == "quantity"
    assert classes["lines[0].gst_rate"] == "quantity"
    assert classes["buyer.name"] == "text"
    assert classes["place_of_supply"] == "text"


def test_a_wrong_value_is_wrong(intra: Perfect) -> None:
    raw = intra.changed()
    raw["lines"][0]["batch_no"]["text"] = "XGX944O68"  # letter O for zero

    score = intra.score(raw)

    assert outcome(score, "lines[0].batch_no") == "wrong"
    assert not score.fully_correct
    wrong = next(field for field in score.fields if field.path == "lines[0].batch_no")
    assert (wrong.expected, wrong.actual) == ("XGX944068", "XGX944O68")


def test_a_null_or_unreadable_value_is_missing(intra: Perfect) -> None:
    raw = intra.changed()
    raw["po_no"] = {"text": None, "block_ids": []}
    raw["invoice_date"]["text"] = "second of September"

    score = intra.score(raw)

    assert outcome(score, "po_no") == "missing"
    assert outcome(score, "invoice_date") == "missing"


def test_numbers_are_compared_by_value_not_by_how_they_are_written(intra: Perfect) -> None:
    raw = intra.changed()
    raw["totals"]["grand_total"]["text"] = "98,697"

    assert outcome(intra.score(raw), "totals.grand_total") == "correct"


def test_a_dropped_line_counts_all_its_fields_as_missing(intra: Perfect) -> None:
    raw = intra.changed()
    raw["lines"].pop()

    score = intra.score(raw)

    last_line = [field for field in score.scored if field.path.startswith("lines[9].")]
    assert len(last_line) == 14
    assert {field.outcome for field in last_line} == {"missing"}
    assert (score.expected_lines, score.extracted_lines) == (10, 9)


def test_an_invented_line_spoils_the_document_but_not_the_field_scores(intra: Perfect) -> None:
    raw = intra.changed()
    raw["lines"].append(copy.deepcopy(raw["lines"][0]))

    score = intra.score(raw)

    assert {field.outcome for field in score.scored} == {"correct"}
    assert (score.expected_lines, score.extracted_lines) == (10, 11)
    assert not score.fully_correct


def test_a_citation_of_the_wrong_block_is_reported_separately(intra: Perfect) -> None:
    raw = intra.changed()
    raw["invoice_no"]["block_ids"] = [intra.block_of["buyer.name"]]
    raw["po_no"]["block_ids"] = []

    score = intra.score(raw)

    cited = {field.path: field.cited for field in score.fields}
    assert outcome(score, "invoice_no") == "correct"
    assert cited["invoice_no"] is False
    assert cited["po_no"] is False
    assert cited["buyer.name"] is True


def test_a_missing_value_has_no_citation_verdict(intra: Perfect) -> None:
    raw = intra.changed()
    raw["po_no"] = {"text": None, "block_ids": []}

    score = intra.score(raw)

    assert next(field for field in score.fields if field.path == "po_no").cited is None


def test_tax_that_does_not_apply_must_stay_null(intra: Perfect, inter: Perfect) -> None:
    assert outcome(intra.score(), "totals.igst") == "correct_null"
    assert outcome(inter.score(), "totals.cgst") == "correct_null"
    assert outcome(inter.score(), "totals.sgst") == "correct_null"
    assert outcome(inter.score(), "totals.igst") == "correct"


def test_a_value_for_tax_that_is_not_printed_is_a_hallucination(intra: Perfect) -> None:
    raw = intra.changed()
    raw["totals"]["igst"] = {"text": "0.00", "block_ids": []}

    score = intra.score(raw)

    assert outcome(score, "totals.igst") == "hallucinated"
    assert not score.fully_correct
    assert "totals.igst" not in {field.path for field in score.scored}


def test_summary_tallies_fields_classes_layouts_and_citations(
    intra: Perfect, inter: Perfect, raw_invoice_from_label: RawFromLabel
) -> None:
    layout_b = Perfect("pair_002", raw_invoice_from_label)
    damaged = intra.changed()
    damaged["lines"][0]["batch_no"]["text"] = "WRONG1234"
    damaged["po_no"] = {"text": None, "block_ids": []}
    damaged["totals"]["igst"] = {"text": "0.00", "block_ids": []}
    scores = [intra.score(damaged), inter.score(), layout_b.score()]

    summary = summarize(scores)

    total = sum(len(score.scored) for score in scores)
    assert summary.documents == 3
    assert summary.documents_fully_correct == 2
    assert summary.line_count_matches == 3
    assert (summary.fields.total, summary.fields.correct) == (total, total - 2)
    assert (summary.fields.wrong, summary.fields.missing) == (1, 1)
    assert summary.fields.accuracy == round((total - 2) / total, 4)
    assert summary.by_class["identifier"].wrong == 1
    assert summary.by_class["identifier"].missing == 1
    assert summary.by_class["amount"].accuracy == 1.0
    assert set(summary.by_layout) == {"A", "B"}
    assert summary.by_layout["B"].accuracy == 1.0
    assert summary.null_expected.total == 4  # igst, cgst+sgst, igst
    assert summary.null_expected.hallucinated == 1
    assert summary.citations.checked == total - 1  # the missing field has nothing to cite
    assert summary.citations.accuracy == 1.0


def test_the_printed_state_code_is_scored(intra: Perfect) -> None:
    raw = intra.changed()
    raw["place_of_supply"]["text"] = "Gujarat (99)"

    score = intra.score(raw)

    assert outcome(intra.score(), "place_of_supply_code") == "correct"
    assert outcome(score, "place_of_supply") == "correct"
    assert outcome(score, "place_of_supply_code") == "wrong"
    assert not score.fully_correct


def test_invented_lines_are_counted_in_the_summary(intra: Perfect) -> None:
    raw = intra.changed()
    raw["lines"] += [copy.deepcopy(raw["lines"][0]), copy.deepcopy(raw["lines"][1])]

    summary = summarize([intra.score(raw), intra.score()])

    assert summary.extra_lines == 2
    assert summary.line_count_matches == 1


def test_a_citation_on_another_page_does_not_count(intra: Perfect) -> None:
    moved = intra.parsed.model_copy(
        update={"blocks": tuple(b.model_copy(update={"page": 2}) for b in intra.parsed.blocks)}
    )
    reply = RawInvoice.model_validate(intra.raw)

    score = score_invoice(intra.label, normalize_invoice(reply, moved), moved)

    assert not any(field.cited for field in score.scored)


def test_a_failed_document_gets_no_credit_for_null_fields(intra: Perfect) -> None:
    score = score_invoice(intra.label, None, intra.parsed, error="model reply rejected")

    assert {field.outcome for field in score.fields} == {"missing"}
    assert summarize([score]).null_expected.total == 0
