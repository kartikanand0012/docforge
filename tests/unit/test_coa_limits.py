"""Reading a specification and checking a result against it."""

from decimal import Decimal

import pytest

from docforge.trust.limits import Limit, check_result, parse_limit, parse_result


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("95.0 - 105.0 %", Limit(low=Decimal("95.0"), high=Decimal("105.0"), unit="%")),
        ("95.0% to 105.0%", Limit(low=Decimal("95.0"), high=Decimal("105.0"), unit="%")),
        ("Between 3.5 and 5.5", Limit(low=Decimal("3.5"), high=Decimal("5.5"), unit="")),
        ("3.5 \u2013 5.5", Limit(low=Decimal("3.5"), high=Decimal("5.5"), unit="")),
        ("NMT 1.0 %", Limit(high=Decimal("1.0"), unit="%")),
        ("Not more than 0.5 EU/mL", Limit(high=Decimal("0.5"), unit="EU/mL")),
        ("≤ 5.0 % w/w", Limit(high=Decimal("5.0"), unit="% w/w")),
        ("NLT 80 % (Q) in 45 min", Limit(low=Decimal("80"), unit="%")),
        ("Not less than 80%", Limit(low=Decimal("80"), unit="%")),
        ("Complies", Limit(text="complies")),
        ("Positive", Limit(text="positive")),
        ("Sterile", Limit(text="sterile")),
    ],
)
def test_specifications_are_read_into_limits(spec: str, expected: Limit) -> None:
    assert parse_limit(spec) == expected


@pytest.mark.parametrize("spec", ["", "see monograph", "95.0 - ", "NMT", "5.5 - 3.5 %"])
def test_a_specification_that_cannot_be_read_gives_no_limit(spec: str) -> None:
    assert parse_limit(spec) is None


@pytest.mark.parametrize(
    ("result", "value", "unit"),
    [
        ("99.4 %", Decimal("99.4"), "%"),
        ("99.4%", Decimal("99.4"), "%"),
        ("0.12 EU/mL", Decimal("0.12"), "EU/mL"),
        ("4.6", Decimal("4.6"), ""),
        ("1.2 % w/w", Decimal("1.2"), "% w/w"),
    ],
)
def test_numeric_results_are_read_with_their_unit(result: str, value: Decimal, unit: str) -> None:
    assert parse_result(result) == (value, unit)


@pytest.mark.parametrize(
    ("spec", "result", "outcome"),
    [
        ("95.0 - 105.0 %", "99.4 %", "passed"),
        ("95.0 - 105.0 %", "105.0 %", "passed"),  # limits are inclusive
        ("95.0 - 105.0 %", "92.4 %", "failed"),
        ("95.0 - 105.0 %", "107.1%", "failed"),
        ("NMT 1.0 %", "0.3 %", "passed"),
        ("NMT 1.0 %", "1.6 %", "failed"),
        ("NLT 80 % (Q) in 45 min", "74 %", "failed"),
        ("NLT 80 % (Q) in 45 min", "93 %", "passed"),
        ("3.5 - 5.5", "6.1", "failed"),
        ("NMT 0.5 EU/mL", "0.9 EU/mL", "failed"),
        ("Complies", "Complies", "passed"),
        ("Sterile", "Complies", "passed"),  # the pharmacopoeial way of reporting it
        ("Positive", "Negative", "failed"),
        ("Complies", "Does not comply", "failed"),
        ("NMT 1.0 %", "1.0 mg", "not_evaluated"),  # a different unit: nothing to compare
        ("NMT 1.0 %", "trace", "not_evaluated"),
        ("see monograph", "Complies", "not_evaluated"),
    ],
)
def test_a_result_is_checked_against_its_specification(
    spec: str, result: str, outcome: str
) -> None:
    assert check_result(spec, result) == outcome
