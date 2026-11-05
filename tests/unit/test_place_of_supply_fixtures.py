"""QA bug 2026-10-09: the place of supply's code held the whole printed text ("Gujarat (24)").
On every synthetic bill, read as a perfect model would copy it, the code is exactly the 2-digit
state code the bill was made with; the state is the name alone."""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.schema import InvoiceExtraction
from docforge.parsing.base import ParsedDocument

FIXTURES = Path(__file__).parents[1] / "fixtures" / "synthetic"
PAIRS = sorted(path.parent.name for path in FIXTURES.glob("pair_*/label.json"))


def test_there_are_fixtures_to_check() -> None:
    assert len(PAIRS) >= 3


@pytest.mark.parametrize("pair", PAIRS)
def test_every_fixture_reads_a_two_digit_code(
    pair: str, raw_invoice_from_label: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    label = json.loads((FIXTURES / pair / "label.json").read_text())
    raw = INVOICE_SPEC.raw_schema.model_validate(raw_invoice_from_label(label))

    parsed = ParsedDocument(parser="fixture", parser_version="1", pages=(), blocks=())
    extraction = INVOICE_SPEC.normalize(raw, parsed)

    assert isinstance(extraction, InvoiceExtraction)
    code = extraction.place_of_supply_code.value
    assert code is not None
    assert re.fullmatch(r"\d{2}", code, re.ASCII)
    assert code == label["invoice"]["place_of_supply_code"]
    assert extraction.place_of_supply.value == label["invoice"]["place_of_supply"]
