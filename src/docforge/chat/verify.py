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


def quote_in(quote: str, passage: str) -> bool:
    wanted = normalise(quote)
    return len(wanted) >= _MIN_QUOTE and wanted in normalise(passage)


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
        # A label such as "Batch" is in many quotes; a value or a phrase places the quote.
        meaningful = len(text) >= _MIN_INSIDE or any(c.isdigit() for c in text)
        inside = meaningful and text in wanted
        if inside or wanted in text or any(run in text for run in runs):
            found.append(block)
    return found
