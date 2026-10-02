"""Printed strings to typed values. This is code, not the model, so it is tested exhaustively."""

from datetime import date
from decimal import Decimal

import pytest

from docforge.extraction.normalize import (
    clean_text,
    parse_date,
    parse_decimal,
    parse_int,
    parse_month,
    parse_place_of_supply,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("02-Sep-2026", date(2026, 9, 2)),
        ("2-sep-2026", date(2026, 9, 2)),
        ("05/06/2026", date(2026, 6, 5)),  # day first: Indian documents
        ("05-06-2026", date(2026, 6, 5)),
        ("2026-06-05", date(2026, 6, 5)),
        (" 05/06/2026 ", date(2026, 6, 5)),
    ],
)
def test_parse_date(raw: str, expected: date) -> None:
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "31/02/2026", "Sep 2026", "05/06/26", "tomorrow", "13-Foo-2026"]
)
def test_parse_date_rejects_what_it_cannot_read(raw: str) -> None:
    assert parse_date(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("06/28", "2028-06"), ("6/28", "2028-06"), ("06/2028", "2028-06"), ("12-27", "2027-12")],
)
def test_parse_month(raw: str, expected: str) -> None:
    assert parse_month(raw) == expected


@pytest.mark.parametrize("raw", ["", "13/28", "00/28", "June 2028", "06/28/2028"])
def test_parse_month_rejects_what_it_cannot_read(raw: str) -> None:
    assert parse_month(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1166.40", "1166.40"),
        ("-0.23", "-0.23"),
        ("98,697.00", "98697.00"),
        ("1,23,456.50", "123456.50"),  # Indian digit grouping
        ("Rs. 59.51", "59.51"),
        ("₹ 59.51", "59.51"),
        ("12%", "12"),
        ("2.5", "2.5"),
        ("0", "0"),
    ],
)
def test_parse_decimal(raw: str, expected: str) -> None:
    value = parse_decimal(raw)

    assert value == Decimal(expected)
    assert str(value) == expected  # printed precision is kept


@pytest.mark.parametrize("raw", ["", "abc", "1.2.3", "12 34", "--5", "1e5", "NaN", "Infinity"])
def test_parse_decimal_rejects_what_it_cannot_read(raw: str) -> None:
    assert parse_decimal(raw) is None


@pytest.mark.parametrize(("raw", "expected"), [("200", 200), ("1,200", 1200), (" 0 ", 0)])
def test_parse_int(raw: str, expected: int) -> None:
    assert parse_int(raw) == expected


@pytest.mark.parametrize("raw", ["", "2.5", "ten", "-3", "10+1"])
def test_parse_int_rejects_what_it_cannot_read(raw: str) -> None:
    assert parse_int(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Gujarat (24)", ("Gujarat", "24")),
        ("Tamil Nadu (33)", ("Tamil Nadu", "33")),
        ("24-Gujarat", ("Gujarat", "24")),
        ("07 - Delhi", ("Delhi", "07")),
        ("Gujarat", ("Gujarat", None)),
    ],
)
def test_parse_place_of_supply(raw: str, expected: tuple[str, str | None]) -> None:
    assert parse_place_of_supply(raw) == expected


def test_clean_text_collapses_whitespace_and_treats_blank_as_missing() -> None:
    assert clean_text("  Jalaram \n Pharmacy ") == "Jalaram Pharmacy"
    assert clean_text("   ") is None
