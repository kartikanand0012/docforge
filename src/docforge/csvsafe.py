"""CSV that is safe to open in a spreadsheet: no cell is read as a formula."""

import csv
import io
import re
from collections.abc import Iterable, Sequence
from typing import Any

# A cell starting with one of these is read as a formula by spreadsheet programs.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


_NUMBER = re.compile(r"-?\d+(\.\d+)?")


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if _NUMBER.fullmatch(text):
        return text  # a plain number, negative or not, is not a formula
    return f"'{text}" if text.startswith(_FORMULA_START) else text


safe_cell = _cell


def write_table(
    header: Sequence[str] | None,
    rows: Iterable[Sequence[Any]],
    *,
    delimiter: str = ",",
    quote_all: bool = False,
    line_end: str = "\n",
) -> str:
    """Rows (and a header, if given) as delimited text in the layout an import file needs,
    every cell safe to open in a spreadsheet."""
    out = io.StringIO()
    writer = csv.writer(
        out,
        delimiter=delimiter,
        quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL,
        lineterminator=line_end,
    )
    if header is not None:
        writer.writerow([_cell(name) for name in header])
    writer.writerows([_cell(value) for value in row] for row in rows)
    return out.getvalue()


def to_csv(rows: Sequence[dict[str, Any]], fieldnames: Sequence[str] | None = None) -> str:
    """Rows as CSV, every cell safe to open in a spreadsheet. With `fieldnames`, the header
    is written even when there are no rows."""
    if not rows and fieldnames is None:
        return ""
    out = io.StringIO()
    names = list(fieldnames) if fieldnames is not None else list(rows[0])
    writer = csv.DictWriter(out, fieldnames=names, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(value) for key, value in row.items()})
    return out.getvalue()
