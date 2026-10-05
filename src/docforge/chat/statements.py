"""Checking an answer statement by statement.

The model answers in statements, each with its citations. A statement is kept only if:

- at least one of its quotes is found in the passage it names (`verify.quote_in`), and
- every figure it states is in one of its found quotes, or in what it repeats (the question,
  the conversation so far). A figure is a number (compared as a number: "₹98,697" is
  98697.00) or a code (an invoice or batch number, a date such as 26-Jun-2026, compared whole).

So one real quote can no longer carry a statement whose figure it does not hold. Its wording
must be the passages' too (most of its own words found there), and each figure must stand
beside what the statement calls it (`_labelled`).
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
    dropped_for_figures: int = 0  # of the statements dropped: quoted, but a figure not held
    dropped_for_wording: int = 0  # of those: saying what its passages do not


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


_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "is",
        "was",
        "were",
        "are",
        "be",
        "been",
        "being",
        "of",
        "for",
        "in",
        "on",
        "to",
        "and",
        "or",
        "by",
        "with",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "as",
        "at",
        "from",
        "which",
        "what",
        "who",
        "whom",
        "whose",
        "there",
        "their",
        "they",
        "them",
        "has",
        "have",
        "had",
        "not",
        "no",
        "but",
        "also",
        "than",
        "then",
        "into",
        "over",
        "under",
        "about",
        "after",
        "before",
        "each",
        "per",
        "any",
        "all",
        "some",
        "said",
        "says",
        "shows",
        "show",
        "given",
        "gives",
        "does",
        "did",
        "done",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        # verbs that only join a value to what it is: they claim nothing of their own
        "come",
        "comes",
        "came",
        "cost",
        "costs",
        "amount",
        "amounts",
        "make",
        "makes",
        "made",
        "stand",
        "stands",
        "equal",
        "equals",
        "read",
        "reads",
        "state",
        "states",
        "list",
        "lists",
        "note",
        "notes",
        "record",
        "records",
    ]
)
# Words a document writes short or another way, read as the word the answer would use.
_ALIAS = {
    "qty": "quantity",
    "amt": "amount",
    "dated": "date",
    "paid": "payment",
    "pay": "payment",
    "pays": "payment",
    # a count of units is a quantity: "billed 20 units" is the "Qty 20" of its row
    "unit": "quantity",
    "units": "quantity",
    "pieces": "quantity",
    "pcs": "quantity",
    "nos": "quantity",
}
_WORD = re.compile(r"[^\W\d_]{3,}")
# A segment: a table cell, a line, a clause, or a sentence. A full stop ends a sentence only
# after a number or a whole word, so "Total Amt. Payable 500" stays one label and its value.
_SEGMENT = re.compile(r"\s*(?:\||\n|;|(?:(?<=[a-z]{4}\.)|(?<=\d\.))\s+(?=[A-Z]))\s*")
_MIN_WORDING = 0.6  # of a statement's own words found in its passages or the conversation


def _words(text: str) -> set[str]:
    """Content words, compared by their first five letters ("issued" is "issue")."""
    found = set()
    for match in _WORD.findall(text):
        word = _ALIAS.get(match.casefold(), match.casefold())
        if len(word) >= 4 and word not in _STOP:
            found.add(word[:5])
    return found


def _wording_supported(statement: str, passages: Sequence[str], given: str) -> bool:
    """Most of the statement's own words are in the passages it cites or in what it
    repeats: an answer cannot add a claim ("and it was paid") its passages do not make."""
    own = _words(CODE.sub(" ", statement))
    if not own:
        return True
    held = _words(" ".join([*passages, given]))
    return len(own & held) / len(own) >= _MIN_WORDING


def _labelled(statement: str, passages: Sequence[Passage], given: str) -> bool:
    """Each figure the statement takes from its passages stands, there, beside a word the
    statement uses for it: "the discount is 98,697" cannot borrow the grand total's figure.

    A table row's first cell (what the row is) counts as beside each of its values, and a
    value alone on its line is read with the line above. The question names a figure only
    when the statement names nothing the passages label ("it comes to 500"): otherwise the
    question's "total" would carry a statement that calls the total the discount."""
    own = _words(CODE.sub(" ", statement))
    numbers = _numbers(_plain(CODE.sub(" ", statement))) - _numbers(_plain(given))
    if not own or not numbers:
        return True
    places: list[tuple[set[Decimal], set[str]]] = []  # the figures of a segment, its label
    for passage in passages:
        segments = [CODE.sub(" ", s) for s in _SEGMENT.split(_plain(passage.text))]
        row = segments[0] if passage.kind == "table_row" and segments else ""
        above = ""
        for segment in segments:
            label = _words(f"{row} {segment}") or _words(above)
            places.append((_numbers(segment), label))
            above = segment
    labels = set().union(*(label for found, label in places if found))
    named = own if own & labels else own | _words(given)
    # A bare value ("Qty 20" once short words go) has no word to weigh: it is not held against it.
    return all(
        any(number in found and (not label or named & label) for found, label in places)
        for number in numbers
    )


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
        read = [passages[i] for i in dict.fromkeys(i for i, _ in found)]
        figures_ok = bool(text and found) and _supported(text, [q for _, q in found], given)
        wording_ok = figures_ok and _wording_supported(text, [p.text for p in read], given)
        if figures_ok and wording_ok and _labelled(text, read, given):
            checked.kept.append(KeptStatement(text=text, citations=tuple(found)))
        else:
            checked.dropped_statements += 1
            if text and found:
                if not figures_ok or wording_ok:
                    checked.dropped_for_figures += 1  # a figure not held, or not beside its label
                else:
                    checked.dropped_for_wording += 1
    return checked
