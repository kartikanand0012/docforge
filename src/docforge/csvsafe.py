"""CSV that is safe to open in a spreadsheet: no cell is read as a formula."""

import csv
import io
import re
from collections.abc import Sequence
from typing import Any

# A cell starting with one of these is read as a formula by spreadsheet programs.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


_NUMBER = re.compile(r"-?\d+(\.\d+)?")


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if _NUMBER.fullmatch(text):
        return text  # a plain number, negative or not, is not a formula
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def to_csv(rows: Sequence[dict[str, Any]]) -> str:
    """Rows as CSV, every cell safe to open in a spreadsheet."""
    if not rows:
        return ""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(value) for key, value in row.items()})
    return out.getvalue()
