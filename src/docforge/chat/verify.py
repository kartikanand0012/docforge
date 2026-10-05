"""Checking an answer's citations against the passages they name.

A citation is a passage number and a quote. It counts only if the quote is in that passage,
compared after normalising what a model may change without changing the meaning: case,
spacing and line breaks, typographic quotes and dashes, full-width forms. The blocks the
quote came from are what the page highlights.
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

_MIN_QUOTE = 4  # characters, after normalising; "8" or "IP" cites nothing
_MIN_INSIDE = 8  # characters: a shorter block inside a quote counts only if it has a digit
_WINDOW = 4  # words: a quote across blocks is placed by any run of this many of its words
_MARKS = str.maketrans(
    {
        "\u2018": "'",  # typographic single quotes
        "\u2019": "'",
        "\u201c": '"',  # typographic double quotes
        "\u201d": '"',
        "\u2013": "-",  # en and em dashes
        "\u2014": "-",
        "\u00ad": "",  # soft hyphen
    }
)


def normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).translate(_MARKS).casefold()
    return re.sub(r"\s+", " ", folded).strip()


def _found(wanted: str, text: str) -> bool:
    """`wanted` in `text`, starting and ending on a boundary: "5,000.00" is not in
    "15,000.00", nor "release" in "released"."""
    start = text.find(wanted)
    while start != -1:
        end = start + len(wanted)
        before_ok = not wanted[0].isalnum() or start == 0 or not text[start - 1].isalnum()
        after_ok = not wanted[-1].isalnum() or end == len(text) or not text[end].isalnum()
        if before_ok and after_ok:
            return True
        start = text.find(wanted, start + 1)
    return False


def _meaningful(wanted: str) -> bool:
    """Enough to show something: a value (with a digit), a phrase, or a long word. A lone
    common word such as "page" or "Date" is in too many passages to show anything."""
    if len(wanted) < _MIN_QUOTE:
        return False
    return any(c.isdigit() for c in wanted) or len(wanted.split()) >= 2 or len(wanted) >= 12


_MAX_PARTS = 3
_MAX_GAP = 400  # characters between parts: within a passage, not across its far ends
_ELLIPSIS = re.compile(r"\s*(?:\[\s*(?:\.\.\.|\u2026)\s*\]|\.\.\.|\u2026)\s*")


def quote_in(quote: str, passage: str, *, max_gap: int | None = _MAX_GAP) -> bool:
    """The quote is in the passage, word for word. A quote that joins parts of the passage
    with an ellipsis ("Invoice X [...] Grand total Y") is found when every part is, in that
    order, close together (one passage row, not across the page), at most three parts, and
    each meaning something: an ellipsis cannot join a batch to another row's amount."""
    text = normalise(passage)
    parts = [normalise(part) for part in _ELLIPSIS.split(quote)]
    parts = [part.strip(" .,;:") for part in parts if part.strip(" .,;:")]
    if not parts or len(parts) > _MAX_PARTS:
        return False
    if len(parts) > 1 and not all(_meaningful(part) or _has_digit_code(part) for part in parts):
        return False
    if len(parts) == 1 and not _meaningful(parts[0]):
        return False
    start = end = 0
    for n, part in enumerate(parts):
        at = _find_from(part, text, start)
        if at == -1 or (n and max_gap is not None and at - end > max_gap):
            return False
        start = end = at + len(part)
    return True


def _has_digit_code(part: str) -> bool:
    return len(part) >= 4 and any(c.isdigit() for c in part) and any(c.isalpha() for c in part)


def _find_from(wanted: str, text: str, start: int) -> int:
    """Where `wanted` is in `text` from `start`, on boundaries; -1 if nowhere."""
    at = text.find(wanted, start)
    while at != -1:
        end = at + len(wanted)
        before_ok = not wanted[0].isalnum() or at == 0 or not text[at - 1].isalnum()
        after_ok = not wanted[-1].isalnum() or end == len(text) or not text[end].isalnum()
        if before_ok and after_ok:
            return at
        at = text.find(wanted, at + 1)
    return -1


def cited_blocks(quote: str, blocks: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The blocks, in order, that the quote was taken from: a block inside the quote, the
    quote inside a block, or a block holding a run of the quote's words."""
    wanted = normalise(quote)
    words = wanted.split()
    runs = {" ".join(words[i : i + _WINDOW]) for i in range(len(words) - _WINDOW + 1)}
    found = []
    for block in blocks:
        text = normalise(str(block.get("text", "")))
        if not text:
            continue
        # A label such as "Batch" is in many quotes, and a line number "1" in any amount;
        # a value or a phrase places the quote.
        has_digit = any(c.isdigit() for c in text)
        meaningful = len(text) >= _MIN_INSIDE or (has_digit and len(text) >= 2)
        inside = meaningful and _found(text, wanted)
        if inside or _found(wanted, text) or any(_found(run, text) for run in runs):
            found.append(block)
    return found
