"""V2 P2 / P3: pre-flight, shared by every import format. A bill that cannot be booked
exactly is refused by name (and by line), with what to correct; nothing is adjusted to fit.
Every bill here is synthetic."""

from typing import Any

from docforge.extraction.schema import InvoiceExtraction
from docforge.preflight import preflight
from profile_fixtures import (
    GUJARAT_SELLER,
    MAHARASHTRA_SELLER,
    bill,
    first_bill,
    invoice,
    line,
    second_bill,
)


def _changed(source: InvoiceExtraction, **changes: Any) -> InvoiceExtraction:
    """`source` with fields replaced by dotted path: `lines.0.batch_no` -> a value."""
    data = source.model_dump(mode="json")
    for path, value in changes.items():
        target: Any = data
        *parents, last = path.split(".")
        for part in parents:
            target = target[int(part)] if part.isdigit() else target[part]
        target[last] = {"value": value, "raw": None, "block_ids": []}
    return InvoiceExtraction.model_validate(data)


def _problems(source: InvoiceExtraction, **kwargs: Any) -> list[tuple[str, int | None]]:
    return [(p.problem, p.line) for p in preflight([bill(source, "b.pdf")], **kwargs)]


def test_bills_that_reconcile_pass() -> None:
    assert _problems(first_bill()) == []
    assert _problems(second_bill()) == []


# AC-P3-3: a medicine line without its batch is refused by bill and line


def test_a_line_without_a_batch_is_refused_by_invoice_and_line_number() -> None:
    problems = _problems(_changed(first_bill(), **{"lines.1.batch_no": None}))

    assert problems == [
        (
            "Invoice SYN/0001, line 2 has no batch number: correct it on the bill, then "
            "export again.",
            2,
        )
    ]


def test_a_line_without_an_expiry_is_refused_too() -> None:
    problems = _problems(_changed(first_bill(), **{"lines.0.expiry": None}))

    assert [number for _, number in problems] == [1]
    assert "has no expiry" in problems[0][0]


def test_a_field_the_layout_requires_is_refused_when_missing() -> None:
    source = _changed(first_bill(), **{"lines.0.mrp": None})

    assert _problems(source) == []
    assert [number for _, number in _problems(source, required=["mrp"])] == [1]


# AC-P3-5: totals reconcile in exact decimals, or the bill is refused by name


def test_a_grand_total_that_does_not_add_up_is_refused_by_name() -> None:
    problems = _problems(_changed(first_bill(), **{"totals.grand_total": "581.00"}))

    assert len(problems) == 1
    assert problems[0][0].startswith("Invoice SYN/0001: Grand total 581.00 is not")


def test_a_paisa_of_rounding_passes_and_two_do_not() -> None:
    within = _changed(first_bill(), **{"totals.round_off": "0.39", "totals.grand_total": "579.99"})
    beyond = _changed(first_bill(), **{"totals.grand_total": "580.02"})

    assert _problems(within) == []
    assert _problems(beyond) != []


def test_a_line_at_the_wrong_rate_is_refused_even_when_the_totals_add_up() -> None:
    # The second line says 12% but its tax (and the bill's) is at 5%.
    problems = _problems(_changed(first_bill(), **{"lines.1.gst_rate": "12"}))

    assert any("the tax at each line's GST rate" in text for text, _ in problems)


def test_tax_rounded_rate_by_rate_is_accepted() -> None:
    # Three lines of 0.10 at 5%: line by line each is 0.01 (0.03); by rate it is 0.02.
    lines = [
        line(product_name=f"Synthetic Item {n}", pack="1x1", hsn="30049099", batch_no=f"B{n}",
             expiry="2028-01", qty=1, free_qty=0, mrp="1.00", ptr="0.10", discount_pct="0",
             taxable_value="0.10", gst_rate="5", amount="0.11")
        for n in range(3)
    ]  # fmt: skip
    source = invoice(
        number="SYN/0003", day="2026-10-07", seller=MAHARASHTRA_SELLER, code="24", lines=lines,
        totals={"taxable_value": "0.30", "igst": "0.02", "round_off": "0.00",
                "grand_total": "0.32"},
    )  # fmt: skip

    assert _problems(source) == []
    wrong = _changed(source, **{"totals.igst": "0.05", "totals.grand_total": "0.35"})
    assert any("GST rate" in text for text, _ in _problems(wrong))


def test_tax_heads_that_do_not_fit_the_supply_are_refused() -> None:
    within = _changed(first_bill(), **{"totals.igst": "39.60", "totals.cgst": None,
                                       "totals.sgst": None})  # fmt: skip
    across = _changed(second_bill(), **{"seller.gstin": GUJARAT_SELLER})

    assert any("where CGST + SGST is due" in text for text, _ in _problems(within))
    assert any("where CGST + SGST is due" in text for text, _ in _problems(across))


def test_a_bill_without_a_place_of_supply_code_cannot_be_split_and_is_refused() -> None:
    problems = _problems(_changed(first_bill(), place_of_supply_code=None))

    assert any("2-digit place-of-supply state code" in text for text, _ in problems)


def test_a_bill_without_its_number_date_or_lines_is_refused() -> None:
    problems = _problems(_changed(first_bill(), invoice_no=None, invoice_date=None))

    assert [text.split(":")[0] for text, _ in problems] == [
        "This bill has no invoice number",
        "This bill has no invoice date",
    ]
    data = first_bill().model_dump(mode="json")
    empty = InvoiceExtraction.model_validate({**data, "lines": []})
    assert any("has no lines" in text for text, _ in _problems(empty))


def test_every_bills_problems_are_listed_not_only_the_first() -> None:
    bad = _changed(first_bill(), **{"lines.0.batch_no": None, "lines.1.batch_no": None})
    worse = _changed(second_bill(), **{"totals.grand_total": "1.00"})

    found = preflight([bill(bad, "a.pdf"), bill(worse, "b.pdf")])

    assert [(p.filename, p.line) for p in found] == [("a.pdf", 1), ("a.pdf", 2), ("b.pdf", None)]
