"""Seeded cases: each has one known defect and says exactly what the checks must report."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.purchase_order import RawPurchaseOrder, normalize_purchase_order
from docforge.extraction.schema import RawInvoice
from docforge.synth import DEFAULT_SEED
from docforge.synth.seeded import DEFECTS, SEEDED_COUNT, build_seeded_case, generate_seeded
from docforge.trust.assess import assess
from docforge.trust.invoice_rules import INVOICE_RULES
from docforge.trust.match import match_invoice_to_order
from fakes import cited, raw_field

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "seeded"
CASES = list(range(1, SEEDED_COUNT + 1))
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def findings(number: int, invoice_of: RawFromLabel, order_of: RawFromLabel) -> set[tuple[str, str]]:
    """What the trust layer reports for a case, given a perfect parser and a faithful model."""
    label, _, _ = build_seeded_case(number, DEFAULT_SEED)
    data = json.loads(label.model_dump_json())
    invoice_raw, order_raw = invoice_of(data), order_of(data)
    invoice_parsed = cited(data, "invoice", invoice_raw)
    order_parsed = cited(data, "purchase_order", order_raw)
    if label.corrupt_reply is not None:  # a model error, simulated
        field = raw_field(invoice_raw, label.corrupt_reply.path)
        assert field is not None
        field["text"] = label.corrupt_reply.text
    invoice = normalize_invoice(RawInvoice.model_validate(invoice_raw), invoice_parsed)
    order = normalize_purchase_order(RawPurchaseOrder.model_validate(order_raw), order_parsed)
    assessment = assess(invoice, invoice_parsed, INVOICE_RULES)
    found = {
        ("rule", rule.rule_id)
        for rule in assessment.rules
        if rule.outcome != "passed" and rule.severity == "error"
    }
    found |= {("verification", f.path) for f in assessment.fields if f.status != "verified"}
    found |= {
        ("discrepancy", d.code)
        for d in match_invoice_to_order(invoice, order)
        if d.severity == "error"
    }
    return found


def test_the_set_covers_every_defect_including_the_three_the_gate_names() -> None:
    defects = [build_seeded_case(number, DEFAULT_SEED)[0].defect for number in CASES]

    assert sorted(defects) == sorted(DEFECTS)
    assert {"wrong_batch", "bad_line_amount", "expired_stock"} <= set(defects)


@pytest.mark.parametrize("number", CASES)
def test_each_case_triggers_exactly_what_its_label_expects(
    number: int, raw_invoice_from_label: RawFromLabel, raw_order_from_label: RawFromLabel
) -> None:
    label, _, _ = build_seeded_case(number, DEFAULT_SEED)

    found = findings(number, raw_invoice_from_label, raw_order_from_label)

    assert found == {(expected.kind, expected.code) for expected in label.expected}
    assert label.expected  # no case is defect-free


def test_cases_are_reproducible_and_distinct_from_the_clean_set() -> None:
    first, first_pdf, _ = build_seeded_case(1, DEFAULT_SEED)
    again, again_pdf, _ = build_seeded_case(1, DEFAULT_SEED)

    assert (first, first_pdf) == (again, again_pdf)
    assert first.pair_id == "case_001"
    clean = json.loads(
        (FIXTURES.parent / "synthetic" / "pair_001" / "label.json").read_text(encoding="utf-8")
    )
    assert first.invoice.invoice_no != clean["invoice"]["invoice_no"]


def test_generation_writes_one_folder_per_case(tmp_path: Path) -> None:
    labels = generate_seeded(tmp_path, DEFAULT_SEED)

    assert [label.pair_id for label in labels] == [f"case_{n:03d}" for n in CASES]
    for label in labels:
        names = {path.name for path in (tmp_path / label.pair_id).iterdir()}
        assert names == {"invoice.pdf", "purchase_order.pdf", "label.json"}
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pairs"] == [label.pair_id for label in labels]


def test_committed_seeded_fixtures_match_a_fresh_generation(tmp_path: Path) -> None:
    generate_seeded(tmp_path, DEFAULT_SEED)

    fresh = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    committed = {
        p.relative_to(FIXTURES): p.read_bytes() for p in FIXTURES.rglob("*") if p.is_file()
    }

    assert fresh.keys() == committed.keys()
    assert [name for name in fresh if fresh[name] != committed[name]] == []
