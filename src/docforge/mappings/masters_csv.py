"""A workspace's existing Tally / Marg names, read from an uploaded CSV to seed suggestions.

The first row names the columns: name and type, and gstin if the file has it, in any order
and case. Each row is a supplier (a ledger or party, with its GSTIN if known) or an item. A
bad row is reported by its line in the file - the header is line 1, as a spreadsheet numbers
it - with the reason; the good rows still count. A name is kept as text, never read as a
formula, and is shown back only through `csvsafe` or JSON.
"""

import csv
import io
import unicodedata
from dataclasses import dataclass
from typing import Literal

from docforge.gstin import is_valid_gstin
from docforge.mappings.keys import gstin_key

Kind = Literal["supplier", "item"]
MAX_ROWS = 20_000
MAX_NAME = 200


class MastersFileInvalid(ValueError):
    """The file cannot be read at all; its message says why, in plain words."""


@dataclass(frozen=True)
class MasterRow:
    line: int
    kind: Kind
    name: str  # spacing collapsed, as typed otherwise
    gstin: str | None


@dataclass(frozen=True)
class RejectedRow:
    line: int
    reason: str


@dataclass(frozen=True)
class ParsedMasters:
    rows: tuple[MasterRow, ...]
    rejected: tuple[RejectedRow, ...]


def printable(text: str) -> bool:
    return not any(unicodedata.category(c).startswith("C") for c in text)


def parse_masters(data: bytes) -> ParsedMasters:
    """Raises `MastersFileInvalid` for a file that is empty, not UTF-8, without a header
    naming name and type, or longer than `MAX_ROWS` rows."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise MastersFileInvalid(
            "The file is not UTF-8 text. Save it from your spreadsheet as CSV UTF-8 and "
            "upload it again."
        ) from error
    if not text.strip():
        raise MastersFileInvalid("The file is empty.")
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = [cell.strip().casefold() for cell in next(reader)]
    except csv.Error as error:
        raise MastersFileInvalid("The file is not CSV that can be read.") from error
    if "name" not in header or "type" not in header:
        raise MastersFileInvalid(
            "The first row must name the columns name and type (and gstin, if you have it)."
        )
    at: dict[str, int] = {
        column: header.index(column) for column in ("name", "type", "gstin") if column in header
    }

    def cell(row: list[str], column: str) -> str:
        index = at.get(column)
        return row[index].strip() if index is not None and index < len(row) else ""

    rows: list[MasterRow] = []
    rejected: list[RejectedRow] = []
    try:
        for row in reader:
            if not any(c.strip() for c in row):
                continue
            if len(rows) + len(rejected) >= MAX_ROWS:
                raise MastersFileInvalid(
                    f"A file may have at most {MAX_ROWS:,} rows; split it and upload each part."
                )
            line = reader.line_num
            found = _row(cell(row, "name"), cell(row, "type"), cell(row, "gstin"), line)
            if isinstance(found, MasterRow):
                rows.append(found)
            else:
                rejected.append(RejectedRow(line, found))
    except csv.Error as error:
        raise MastersFileInvalid(
            f"The file stops being CSV that can be read at line {reader.line_num}."
        ) from error
    return ParsedMasters(tuple(rows), tuple(rejected))


def _row(name: str, kind: str, gstin: str, line: int) -> MasterRow | str:
    """The row, or why it is refused."""
    name = " ".join(name.split())
    if not name:
        return "The name is empty."
    if not printable(name):
        return "The name has characters that cannot be shown."
    if len(name) > MAX_NAME:
        return f"The name is longer than {MAX_NAME} characters."
    kind = kind.casefold()
    if kind == "item":
        return (
            "An item has no GSTIN; leave it empty."
            if gstin
            else MasterRow(line, "item", name, None)
        )
    if kind != "supplier":
        return "The type must be supplier or item."
    if not gstin:
        return MasterRow(line, "supplier", name, None)
    key = gstin_key(gstin)
    if key is None or not is_valid_gstin(key):
        return "The GSTIN is not valid."
    return MasterRow(line, "supplier", name, key)
