"""Printed strings to typed values. This is code, not the model, so it is tested exhaustively."""

import re
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
def test_parse_place_of_supply(raw: str, expected: tuple[str | None, str | None]) -> None:
    assert parse_place_of_supply(raw) == expected


def test_clean_text_collapses_whitespace_and_treats_blank_as_missing() -> None:
    assert clean_text("  Jalaram \n Pharmacy ") == "Jalaram Pharmacy"
    assert clean_text("   ") is None


@pytest.mark.parametrize(
    "raw",
    [
        "12,50",  # decimal comma: must not become 1250
        "1,2",
        "1,234,56.00",
        "1Rs2",  # a currency mark in the middle of the digits
        "5%5",
        "\u0663\u0664.\u0665",  # Arabic-Indic digits
    ],
)
def test_parse_decimal_rejects_ambiguous_grouping_and_foreign_digits(raw: str) -> None:
    assert parse_decimal(raw) is None


@pytest.mark.parametrize("raw", ["1,2", "12,50", "\u0663\u0664"])
def test_parse_int_rejects_ambiguous_grouping_and_foreign_digits(raw: str) -> None:
    assert parse_int(raw) is None


@pytest.mark.parametrize(("raw", "expected"), [("1,200", 1200), ("12,34,567", 1234567)])
def test_parse_int_accepts_western_and_indian_grouping(raw: str, expected: int) -> None:
    assert parse_int(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("7.00", 7), ("7.0", 7), ("1,200.00", 1200), ("12,34,567.00", 1234567), ("0.00", 0)],
)
def test_parse_int_reads_a_count_printed_with_a_zero_fraction(raw: str, expected: int) -> None:
    # Many invoices print quantities to two places ("7.00"); that is still a whole count.
    assert parse_int(raw) == expected


@pytest.mark.parametrize(
    "raw", ["7.5", "7.50", "7.05", "7.", ".00", "7.00.00", "-7.00", "12,50.00"]
)
def test_parse_int_does_not_round_a_fractional_count(raw: str) -> None:
    assert parse_int(raw) is None


@pytest.mark.parametrize("raw", ["06/0028", "06/028", "\uff10\uff16/28"])
def test_parse_month_rejects_odd_years_and_foreign_digits(raw: str) -> None:
    assert parse_month(raw) is None


@pytest.mark.parametrize("raw", ["02-09/2026", "\uff12-Sep-2026", "02/Sep-2026"])
def test_parse_date_rejects_mixed_separators_and_foreign_digits(raw: str) -> None:
    assert parse_date(raw) is None


def test_a_bare_code_is_not_taken_for_a_state_name() -> None:
    assert parse_place_of_supply("24") == (None, "24")
    assert parse_place_of_supply("Gujarat - 24") == ("Gujarat", "24")


# QA bug 2026-10-09: the place of supply's code must be exactly a 2-digit GST state code or
# nothing, whatever the bill prints around it. A code is never the whole printed text.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Gujarat [24]", ("Gujarat", "24")),
        ("Gujarat (Code: 24)", ("Gujarat", "24")),
        ("Gujarat (State Code: 24)", ("Gujarat", "24")),
        ("Gujarat (State Code - 24)", ("Gujarat", "24")),
        ("State: Gujarat, Code: 24", ("Gujarat", "24")),
        ("State Name: Gujarat State Code: 24", ("Gujarat", "24")),
        ("Gujarat, 24", ("Gujarat", "24")),
        ("Gujarat / 24", ("Gujarat", "24")),
        ("(24) Gujarat", ("Gujarat", "24")),
        ("24 Gujarat", ("Gujarat", "24")),
        ("Gujarat 24", ("Gujarat", "24")),
        ("97-Other Territory", ("Other Territory", "97")),
        ("96 - Other Countries", ("Other Countries", "96")),
        ("Ladakh (38)", ("Ladakh", "38")),
    ],
)
def test_parse_place_of_supply_reads_the_code_however_it_is_printed(
    raw: str, expected: tuple[str | None, str | None]
) -> None:
    assert parse_place_of_supply(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Not a GST state code: no code rather than a wrong one.
        ("Gujarat (99)", ("Gujarat", None)),
        ("Gujarat (00)", ("Gujarat", None)),
        ("Gujarat - 45", ("Gujarat", None)),
        ("99", (None, None)),
        # One digit, three digits or foreign digits are not a 2-digit code.
        ("Delhi (7)", ("Delhi (7)", None)),
        ("Gujarat (024)", ("Gujarat (024)", None)),
        ("Gujarat (\uff12\uff14)", ("Gujarat (\uff12\uff14)", None)),
    ],
)
def test_parse_place_of_supply_never_takes_what_is_not_a_state_code(
    raw: str, expected: tuple[str | None, str | None]
) -> None:
    assert parse_place_of_supply(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Gujarat (24)", "Gujarat [24]", "State: Gujarat, Code: 24", "Gujarat", "24",
        "Gujarat (99)", "Delhi (7)", "", " ", "(", "Code:", "24-", "-24", "Gujarat (24) (27)",
        "27-Maharashtra (24)", "Maharashtra, Mumbai 400001", "PIN 380015", "Gujarat 380015",
    ],
)  # fmt: skip
def test_a_place_of_supply_code_is_two_digits_or_nothing(raw: str) -> None:
    _, code = parse_place_of_supply(raw)
    assert code is None or re.fullmatch(r"\d{2}", code, re.ASCII)
    assert code is None or code in raw
