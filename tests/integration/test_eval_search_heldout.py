"""The held-out search set: questions in forms the search was never tuned on, scored by rank.

Codes with an OCR-style misread, codes written another way, everyday wording, a supplier's
GSTIN that names several invoices, and codes that are in no document. Recall@1, recall@5 and
mean reciprocal rank per kind; for unanswerable questions, how often a mode returns nothing.
"""

import json
from pathlib import Path

import pytest
from docforge.evals.search_heldout import (
    HeldoutReport,
    build_heldout_questions,
    run_heldout_eval,
)

from docforge.config import get_settings
from docforge.search.embeddings import FakeEmbedder, RecordingEmbedder

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
SYNTHETIC = REPO / "tests" / "fixtures" / "synthetic"
COA = REPO / "tests" / "fixtures" / "coa"
RECORDED = REPO / "tests" / "fixtures" / "recorded"
LABELS = [json.loads(p.read_text()) for p in sorted(SYNTHETIC.glob("*/label.json"))] + [
    json.loads(p.read_text()) for p in sorted(COA.glob("*/label.json"))
]


def test_the_held_out_kinds_are_all_there_and_none_is_a_tuned_template() -> None:
    questions = build_heldout_questions(SYNTHETIC, COA)

    assert {q.kind for q in questions} == {
        "ocr_code", "code_variant", "natural", "multi", "no_answer"
    }  # fmt: skip
    for tuned in (
        "Which invoice billed batch",
        "invoice number",
        "purchase order",
        "certificate of analysis for batch",
    ):
        assert not any(q.text.startswith(tuned) for q in questions), tuned
    assert build_heldout_questions(SYNTHETIC, COA) == questions  # repeatable


def test_an_ocr_code_differs_from_the_printed_code_by_one_character() -> None:
    questions = [q for q in build_heldout_questions(SYNTHETIC, COA) if q.kind == "ocr_code"]
    printed = {label["invoice"]["lines"][0]["batch_no"] for label in LABELS if "invoice" in label}

    assert questions
    for question in questions:
        code = question.text.split()[-1]
        assert code not in printed
        assert any(
            len(code) == len(p) and sum(a != b for a, b in zip(code, p, strict=True)) == 1
            for p in printed
        )


def test_unanswerable_codes_are_in_no_document() -> None:
    every_label = json.dumps(LABELS)
    for question in build_heldout_questions(SYNTHETIC, COA):
        if question.kind == "no_answer":
            assert question.expected == ()
            assert question.text.split()[-1] not in every_label


def test_a_small_run_scores_by_rank_and_finds_nothing_across_tenants(
    empty_database_url: object,
) -> None:
    report = run_heldout_eval(
        SYNTHETIC, COA, RECORDED, FakeEmbedder(), pairs=2, database_url=empty_database_url
    )

    for mode in report.modes.values():
        assert 0.0 <= mode.recall_at_1 <= mode.recall_at_5 <= 1.0
        assert 0.0 <= mode.mrr <= 1.0
        assert 0.0 <= mode.abstained_when_no_answer <= 1.0
        assert mode.cross_tenant_hits == 0


def test_the_committed_held_out_report_is_reproduced_offline() -> None:
    model = get_settings().embedding_model
    report = run_heldout_eval(
        SYNTHETIC, COA, RECORDED, RecordingEmbedder(RECORDED / "embeddings", None, model=model)
    )

    committed = HeldoutReport.model_validate_json(
        (REPO / "evals" / "baselines" / "search_heldout.json").read_text()
    )
    assert report == committed
