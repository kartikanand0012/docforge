"""Checking an answer statement by statement.

The model answers in statements, each with its citations. A statement is kept only if:

- at least one of its quotes is found in the passage it names (`verify.quote_in`), and
- every figure it states is in one of its found quotes, or in what it repeats (the question,
  the conversation so far). A figure is a number (compared as a number: "₹98,697" is
  98697.00) or a code (an invoice or batch number, a date such as 26-Jun-2026, compared whole).

So one real quote can no longer carry a statement whose figure it does not hold. What this
does not check is wording without figures ("it was approved"): such a statement still stands
on its quote being found.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel

from docforge.chat.prompt import Passage
from docforge.chat.verify import normalise, quote_in

# A number: Western or Indian digit grouping (98,697.00, 1,23,456.00) or none, with its sign
# when it stands alone ("-500", or with the minus sign U+2212). "1,2,3" is three numbers.
_NUMBER = re.compile(
    r"(?:(?<![\w.])(?P<sign>[-\u2212]))?(?<![\d])(?<!\d\.)"
    r"(?P<digits>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
)
# Letters with digits, or digits with / or - inside: an invoice or batch number, a date.
CODE = re.compile(r"\b(?=[\w/.-]*\d)(?=[\w/.-]*[A-Za-z/-])[\w][\w/.-]*[\w]")
# A number written against its unit ("250mg", "10ml", "Rs.500"): the number, not a code.
_UNIT = re.compile(r"(\d)([A-Za-z]{1,3})\b")
_PREFIX = re.compile(r"\b([A-Za-z]{1,3})\.(?=\d)")


class RawCitation(BaseModel):  # no extra="forbid": Gemini's schema has no additionalProperties
    passage: int
    quote: str


class RawStatement(BaseModel):
    text: str
    citations: list[RawCitation]


@dataclass(frozen=True)
class KeptStatement:
    text: str
    citations: tuple[tuple[int, str], ...]  # (passage index from 0, quote), found


@dataclass
class Checked:
    kept: list[KeptStatement] = field(default_factory=list)
    dropped_statements: int = 0
    dropped_citations: int = 0


def _plain(text: str) -> str:
    """Units and currency prefixes set apart from their numbers: "250mg" is 250 mg."""
    return _PREFIX.sub(r"\1 ", _UNIT.sub(r"\1 \2", text))


def _numbers(text: str) -> set[Decimal]:
    found = set()
    for match in _NUMBER.finditer(text):
        try:
            value = Decimal(match["digits"].replace(",", "")).normalize()
        except InvalidOperation:
            continue
        found.add(-value if match["sign"] else value)
    return found


def _codes(text: str) -> set[str]:
    # Compared without spaces: "95.0-105.0" is "95.0 - 105.0".
    return {re.sub(r"\s+", "", normalise(code)) for code in CODE.findall(text)}


def _squashed(text: str) -> str:
    return re.sub(r"\s+", "", normalise(text))


def _supported(statement: str, quotes: Sequence[str], given: str) -> bool:
    """Every figure of the statement is in its quotes or in what it repeats, and at least one
    is in its quotes: a figure only repeated from the question ("is the total 5,000?") does
    not stand on its own. Figures are matched against the quotes together, not each against
    the label beside it."""
    statement = _plain(statement)
    quoted, repeated = _plain(" ".join(quotes)), _plain(given)
    codes = _codes(statement)
    numbers = _numbers(CODE.sub(" ", statement))  # a code's own digits go with the code
    if not codes and not numbers:
        return True
    in_quotes, in_given = _squashed(quoted), _squashed(repeated)
    quote_numbers, given_numbers = _numbers(quoted), _numbers(repeated)
    from_quotes = {c for c in codes if c in in_quotes} | (numbers & quote_numbers)
    from_given = {c for c in codes if c in in_given} | (numbers & given_numbers)
    every = (codes | numbers) <= (from_quotes | from_given)
    return every and bool(from_quotes)


def check_statements(
    statements: Sequence[RawStatement], passages: Sequence[Passage], *, given: str
) -> Checked:
    checked = Checked()
    for statement in statements:
        found: list[tuple[int, str]] = []
        for cited in statement.citations:
            index = cited.passage - 1
            # A summary is DocForge's own account of one document: a quote may join any of
            # its parts. Elsewhere the parts must sit close together.
            passage = passages[index] if 0 <= index < len(passages) else None
            gap = None if passage is not None and passage.kind == "summary" else 400
            if passage is not None and quote_in(cited.quote, passage.text, max_gap=gap):
                found.append((index, cited.quote))
            else:
                checked.dropped_citations += 1
        text = statement.text.strip()
        if text and found and _supported(text, [q for _, q in found], given):
            checked.kept.append(KeptStatement(text=text, citations=tuple(found)))
        else:
            checked.dropped_statements += 1
    return checked
