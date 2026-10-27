"""V2 P1: a workspace's existing Tally / Marg names, uploaded as CSV to seed suggestions.

Columns: name, gstin (optional, suppliers only), type (supplier or item). Each bad row is
reported by its line in the file (the header is line 1, as a spreadsheet numbers it) with a
reason in plain words; the good rows still count.
"""

import pytest

from docforge.mappings.masters_csv import MastersFileInvalid, parse_masters

GSTIN = "24AABFN7754K1Z0"


def _csv(*rows: str, header: str = "name,gstin,type") -> bytes:
    return ("\n".join((header, *rows)) + "\n").encode()


def test_good_rows_are_read_with_their_kind_and_gstin() -> None:
    parsed = parse_masters(
        _csv(f"Navjivan Medical Agencies,{GSTIN},supplier", "DOLO 650 TAB,,item")
    )

    assert [(r.line, r.kind, r.name, r.gstin) for r in parsed.rows] == [
        (2, "supplier", "Navjivan Medical Agencies", GSTIN),
        (3, "item", "DOLO 650 TAB", None),
    ]
    assert parsed.rejected == ()


def test_each_bad_row_is_reported_by_its_line_and_the_rest_are_kept() -> None:
    parsed = parse_masters(
        _csv(
            "ABC Pharma,,supplier",  # 2: good
            ",,item",  # 3: no name
            "XYZ Traders,,vendor",  # 4: not a type
            "Bad GSTIN Co,24AABFN7754K1Z9,supplier",  # 5: GSTIN check digit wrong
            "Cefixime 200 Tab,,ITEM",  # 6: good, any case
            f"Some Tab,{GSTIN},item",  # 7: an item has no GSTIN
            "=HYPERLINK(1),,supplier",  # 8: good: kept as text, never a formula
        )
    )

    assert [r.line for r in parsed.rows] == [2, 6, 8]
    assert [(r.line, r.reason) for r in parsed.rejected] == [
        (3, "The name is empty."),
        (4, "The type must be supplier or item."),
        (5, "The GSTIN is not valid."),
        (7, "An item has no GSTIN; leave it empty."),
    ]


def test_columns_may_come_in_any_order_and_gstin_may_be_left_out() -> None:
    parsed = parse_masters(_csv("item,Dolo 650 Tab", header="Type,Name"))

    assert [(r.kind, r.name, r.gstin) for r in parsed.rows] == [("item", "Dolo 650 Tab", None)]


def test_a_spreadsheets_byte_order_mark_and_blank_lines_are_ignored() -> None:
    data = "﻿name,type\r\nABC Pharma,supplier\r\n\r\nDolo,item\r\n".encode()

    parsed = parse_masters(data)

    assert [(r.line, r.name) for r in parsed.rows] == [(2, "ABC Pharma"), (4, "Dolo")]


def test_a_name_too_long_is_a_bad_row() -> None:
    parsed = parse_masters(_csv(f"{'A' * 201},,supplier"))

    assert [(r.line, r.reason) for r in parsed.rejected] == [
        (2, "The name is longer than 200 characters.")
    ]


@pytest.mark.parametrize(
    ("data", "says"),
    [
        (b"", "The file is empty."),
        (b"ledger,kind\nABC,supplier\n", "The first row must name the columns name and type"),
        (b"name,type\n\xff\xfe,supplier\n", "The file is not UTF-8 text"),
    ],
)
def test_a_file_that_cannot_be_read_says_why(data: bytes, says: str) -> None:
    with pytest.raises(MastersFileInvalid, match=says):
        parse_masters(data)


def test_a_file_of_more_rows_than_allowed_is_refused_whole() -> None:
    rows = [f"Item {n},,item" for n in range(20_001)]

    with pytest.raises(MastersFileInvalid, match="at most 20,000 rows"):
        parse_masters(_csv(*rows))
