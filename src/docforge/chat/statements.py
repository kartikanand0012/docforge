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

_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
# Letters with digits, or digits with / or - inside: an invoice or batch number, a date.
_CODE = re.compile(r"\b(?=[\w/.-]*\d)(?=[\w/.-]*[A-Za-z/-])[\w][\w/.-]*[\w]")


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


def _numbers(text: str) -> set[Decimal]:
    found = set()
    for token in _NUMBER.findall(text):
        try:
            found.add(Decimal(token.replace(",", "")).normalize())
        except InvalidOperation:
            continue
    return found


def _codes(text: str) -> set[str]:
    return {normalise(code) for code in _CODE.findall(text)}


def _supported(statement: str, quotes: Sequence[str], given: str) -> bool:
    """Every figure of the statement is in its quotes or in what it repeats."""
    held = " ".join([*quotes, given])
    if not _codes(statement) <= _codes(held):
        return False
    # A number that is part of a code (2026 in NVM/26-27/32001) is checked with its code.
    return _numbers(_CODE.sub(" ", statement)) <= _numbers(held)


def check_statements(
    statements: Sequence[RawStatement], passages: Sequence[Passage], *, given: str
) -> Checked:
    checked = Checked()
    for statement in statements:
        found: list[tuple[int, str]] = []
        for cited in statement.citations:
            index = cited.passage - 1
            if 0 <= index < len(passages) and quote_in(cited.quote, passages[index].text):
                found.append((index, cited.quote))
            else:
                checked.dropped_citations += 1
        text = statement.text.strip()
        if text and found and _supported(text, [q for _, q in found], given):
            checked.kept.append(KeptStatement(text=text, citations=tuple(found)))
        else:
            checked.dropped_statements += 1
    return checked
