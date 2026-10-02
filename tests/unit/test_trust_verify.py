"""Checking each extracted value against the text of the blocks it cites."""

from typing import Any

from pydantic import BaseModel

from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.schema import Extracted, RawInvoice
from docforge.parsing.base import BBox, Block, Page, ParsedDocument
from docforge.trust.verify import FieldCheck, verify_extraction

EMPTY: dict[str, Any] = {"text": None, "block_ids": []}
LINE_FIELDS = (
    "product_name",
    "pack",
    "hsn",
    "batch_no",
    "mfg",
    "expiry",
    "qty",
    "free_qty",
    "mrp",
    "ptr",
    "discount_pct",
    "taxable_value",
    "gst_rate",
    "amount",
)


def parsed(*texts: str) -> ParsedDocument:
    return ParsedDocument(
        parser="fake",
        parser_version="0",
        pages=(Page(number=1, width=600, height=800),),
        blocks=tuple(
            Block(
                id=f"b{n}",
                kind="text",
                text=text,
                page=1,
                bbox=BBox(x0=10, y0=10 * n, x1=200, y1=10 * n + 8),
            )
            for n, text in enumerate(texts, start=1)
        ),
    )


def field(text: str, *block_ids: str) -> dict[str, Any]:
    return {"text": text, "block_ids": list(block_ids)}


def checks(document: ParsedDocument, **fields: dict[str, Any]) -> dict[str, FieldCheck]:
    """Verify an invoice whose header fields are `fields`; everything else is absent."""
    party: dict[str, Any] = {
        "name": EMPTY,
        "address": EMPTY,
        "gstin": EMPTY,
        "drug_licence_nos": [],
    }
    line = fields.pop("line", None)
    raw: dict[str, Any] = {
        "invoice_no": EMPTY,
        "invoice_date": EMPTY,
        "po_no": EMPTY,
        "po_date": EMPTY,
        "place_of_supply": EMPTY,
        "seller": {**party, **fields.pop("seller", {})},
        "buyer": party,
        "lines": [{**dict.fromkeys(LINE_FIELDS, EMPTY), **line}] if line else [],
        "totals": dict.fromkeys(
            ("taxable_value", "cgst", "sgst", "igst", "round_off", "grand_total"), EMPTY
        ),
        **fields,
    }
    extraction = normalize_invoice(RawInvoice.model_validate(raw), document)
    return {check.path: check for check in verify_extraction(extraction, document)}


def test_a_value_found_in_its_cited_block_is_verified_and_gets_that_blocks_box() -> None:
    document = parsed("TAX INVOICE", "Invoice No.: NVM/26-27/32001")

    result = checks(document, invoice_no=field("NVM/26-27/32001", "b2"))["invoice_no"]

    assert result.status == "verified"
    assert [(box.page, box.x0, box.y0, box.x1, box.y1) for box in result.boxes] == [
        (1, 10, 20, 200, 28)
    ]


def test_absent_fields_are_not_checked() -> None:
    result = checks(parsed("Invoice No.: A1"), invoice_no=field("A1", "b1"))

    assert set(result) == {"invoice_no"}


def test_a_value_that_differs_from_its_cited_text_is_flagged() -> None:
    """The gate's "wrong batch": the model returns O where the page has 0."""
    document = parsed("XGX944068", "10x10")

    result = checks(document, line={"batch_no": field("XGX944O68", "b1")})["lines[0].batch_no"]

    assert result.status == "not_in_cited_blocks"
    assert result.found_in == ()
    assert len(result.boxes) == 1  # the reviewer is still shown where the model pointed


def test_a_value_cited_to_the_wrong_block_reports_where_it_actually_is() -> None:
    document = parsed("Round Off", "-0.23", "Grand Total")

    result = checks(document, invoice_no=field("-0.23", "b1"))["invoice_no"]

    assert result.status == "not_in_cited_blocks"
    assert result.found_in == ("b2",)


def test_a_value_with_no_citation_is_flagged() -> None:
    result = checks(parsed("Invoice No.: A1"), invoice_no=field("A1"))["invoice_no"]

    assert result.status == "no_citation"
    assert result.found_in == ("b1",)
    assert result.boxes == ()


def test_a_number_inside_a_longer_number_does_not_count() -> None:
    document = parsed("200", "1,166.40", "86.245")

    result = checks(
        document,
        line={
            "qty": field("20", "b1"),
            "taxable_value": field("166.40", "b2"),
            "mrp": field("86.24", "b3"),
        },
    )

    assert {check.status for check in result.values()} == {"not_in_cited_blocks"}


def test_a_value_sharing_a_block_with_a_label_or_serial_number_is_verified() -> None:
    document = parsed("GSTIN: 24AABFN7754K1Z0", "1 Amoxicillin Capsules IP 250mg", "Gujarat (24)")

    result = checks(
        document,
        seller={"gstin": field("24AABFN7754K1Z0", "b1")},
        line={"product_name": field("Amoxicillin Capsules IP 250mg", "b2")},
        place_of_supply=field("Gujarat (24)", "b3"),
    )

    assert {check.status for check in result.values()} == {"verified"}
    assert set(result) == {
        "seller.gstin",
        "lines[0].product_name",
        "place_of_supply",
        "place_of_supply_code",
    }


def test_spacing_differences_do_not_matter() -> None:
    document = parsed("Shreeram   Pharma\nDistributors")

    result = checks(document, seller={"name": field("Shreeram Pharma Distributors", "b1")})

    assert result["seller.name"].status == "verified"


def test_a_value_spread_over_two_cited_blocks_is_verified() -> None:
    document = parsed("14 Market Yard Road,", "Pune 411037")

    result = checks(
        document, seller={"address": field("14 Market Yard Road, Pune 411037", "b1", "b2")}
    )["seller.address"]

    assert result.status == "verified"
    assert len(result.boxes) == 2


def test_an_empty_value_is_never_verified() -> None:
    """The normaliser turns a blank into an absent value; the verifier must not rely on that."""
    blank = Extracted[str](value="", raw="  ", block_ids=("b1",))

    class OneField(BaseModel):
        batch_no: Extracted[str]

    (result,) = verify_extraction(OneField(batch_no=blank), parsed("Batch: "))

    assert result.status == "not_in_cited_blocks"


def test_a_short_number_must_be_the_whole_cited_block() -> None:
    """ "5" inside "Qty 10 Free 5" proves nothing about which value it is."""
    document = parsed("Qty 10 Free 5", "5", "-5", "(5.00)")

    def status(text: str, block: str) -> str:
        return checks(document, line={"free_qty": field(text, block)})["lines[0].free_qty"].status

    assert status("5", "b1") == "not_in_cited_blocks"
    assert status("5", "b2") == "verified"
    assert status("5", "b3") == "not_in_cited_blocks"  # the sign was dropped


def test_a_number_is_not_verified_against_its_negative_or_bracketed_form() -> None:
    document = parsed("Round Off -0.23", "(125.00)")

    result = checks(
        document,
        line={"mrp": field("0.23", "b1"), "ptr": field("125.00", "b2")},
    )

    assert {check.status for check in result.values()} == {"not_in_cited_blocks"}


def test_a_number_cannot_be_assembled_from_two_cited_blocks() -> None:
    document = parsed("Total 10", "5 items")

    result = checks(
        document, invoice_no=field("10 5", "b1", "b2"), line={"qty": field("105", "b1", "b2")}
    )

    assert result["lines[0].qty"].status == "not_in_cited_blocks"


def test_a_short_number_in_a_block_of_merged_numeric_cells_is_verified() -> None:
    """The parser sometimes merges two cells, "2" and "12", into one block "2 12"."""
    document = parsed("2 12", "Disc 2 GST 12")

    def status(text: str, block: str) -> str:
        return checks(document, line={"gst_rate": field(text, block)})["lines[0].gst_rate"].status

    assert status("12", "b1") == "verified"
    assert status("2", "b1") == "verified"
    assert status("1", "b1") == "not_in_cited_blocks"
    assert status("12", "b2") == "not_in_cited_blocks"  # labels present: which number is it?


def test_part_of_a_date_or_a_hyphenated_number_does_not_count() -> None:
    document = parsed("Exp 05/29/2028", "Exp 05/29", "NVM/26-27/32001")

    def status(text: str, block: str) -> str:
        return checks(document, line={"expiry": field(text, block)})["lines[0].expiry"].status

    assert status("05/29", "b1") == "not_in_cited_blocks"
    assert status("05/29", "b2") == "verified"
    assert status("32001", "b3") == "not_in_cited_blocks"
    assert status("NVM/26-27/32001", "b3") == "verified"
