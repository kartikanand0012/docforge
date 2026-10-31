"""The definition of "the format is right" (V2 P3): from the same bills, the engine makes the
import file the books accepted, cell for cell.

`compare` reads the accepted file in the profile's layout and the engine's rows for the same
bills, and reports each cell that differs. A CSV cell is equal when its text is; an XLSX cell
the spreadsheet holds as a number or a date is equal when the value is (12.5 and "12.50").
"""

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import islice
from typing import Any

from docforge.csvsafe import safe_cell
from docforge.profiles.render import Cell, headers
from docforge.profiles.spec import MAX_COLUMNS, Profile

MAX_DIFFS = 200
MAX_ROWS = 20_000  # far more than the bills a check compares; a larger file is refused
_MAX_WIDTH = MAX_COLUMNS + 1  # read one past, to tell a file that is too wide


class SampleUnreadable(ValueError):
    """The accepted file cannot be read in the profile's layout."""


@dataclass(frozen=True)
class CellDiff:
    row: int  # the file's row, from 1 (the header is row 1 when there is one)
    column: str  # its header, or its place ("column 3")
    expected: str  # as the accepted file has it
    produced: str  # as the engine writes it


@dataclass(frozen=True)
class Comparison:
    rows_expected: int  # without the header
    rows_produced: int
    cells: int
    equal: int
    diffs: tuple[CellDiff, ...]  # the first MAX_DIFFS

    @property
    def exact(self) -> bool:
        return self.rows_expected == self.rows_produced and self.cells == self.equal


def read_rows(profile: Profile, data: bytes) -> list[list[Any]]:
    """The accepted file's rows as written, header included, trailing empty rows dropped."""
    if profile.file_type == "xlsx":
        rows = _xlsx_rows(data, profile.sheet_name)
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise SampleUnreadable("The file is not UTF-8 text.") from error
        reader = csv.reader(io.StringIO(text), delimiter=profile.delimiter)
        try:
            rows = [row[:_MAX_WIDTH] for row in islice(reader, MAX_ROWS + 1)]
        except csv.Error as error:
            raise SampleUnreadable(f"The file cannot be read as CSV: {error}.") from error
    if len(rows) > MAX_ROWS:
        raise SampleUnreadable(f"The file has more than {MAX_ROWS:,} rows: a few bills are enough.")
    while rows and all(v is None or v == "" for v in rows[-1]):
        rows.pop()
    return rows


def _trimmed(row: tuple[Any, ...]) -> list[Any]:
    """A sheet's row without the empty cells read past its end."""
    cells = list(row)
    while cells and cells[-1] is None:
        cells.pop()
    return cells


MAX_UNPACKED = 50 * 1024 * 1024
MAX_PARTS = 500


def check_workbook(data: bytes) -> None:
    """Refuse a workbook that unpacks to far more than its size: 2 MB can hold 2 GB."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            parts = archive.infolist()
    except (zipfile.BadZipFile, ValueError) as error:
        raise SampleUnreadable("The file is not a workbook Excel can open.") from error
    if len(parts) > MAX_PARTS or sum(p.file_size for p in parts) > MAX_UNPACKED:
        raise SampleUnreadable(
            "The workbook unpacks to more than 50 MB: save just the import sheet and upload it."
        )


def _xlsx_rows(data: bytes, sheet_name: str) -> list[list[Any]]:
    from openpyxl import load_workbook

    check_workbook(data)
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as error:  # a bad zip, or not a workbook
        raise SampleUnreadable("The file is not a workbook Excel can open.") from error
    try:
        sheet = workbook[sheet_name] if sheet_name in workbook.sheetnames else workbook.active
        if sheet is None:
            raise SampleUnreadable("The workbook has no sheet.")
        if (sheet.max_column or 0) > MAX_COLUMNS:
            raise SampleUnreadable(f"The file has more than {MAX_COLUMNS} columns.")
        # Bounded whatever size the sheet claims: a small file can unpack to a vast one.
        found = sheet.iter_rows(max_row=MAX_ROWS + 1, max_col=_MAX_WIDTH, values_only=True)
        return [_trimmed(row) for row in found]
    finally:
        workbook.close()


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value).strip()
    try:
        return Decimal(text) if text else None
    except InvalidOperation:
        return None


def _written(cell: Cell, xlsx: bool) -> str:
    """The cell's text as it is in the file: a CSV cell made safe from formulas."""
    return cell.text if xlsx else safe_cell(cell.text)


def _same(expected: Any, produced: Cell, xlsx: bool, cell: str = "text") -> bool:
    """Equal as the file holds them. In an Excel file a cell's kind is part of the format:
    a date or number cell is equal only to a date or number cell of the same value."""
    text = _written(produced, xlsx)
    if expected is None:
        return text == ""
    if isinstance(expected, str):
        return expected == text
    if isinstance(expected, datetime):
        expected = expected.date()
    if isinstance(expected, date):
        return cell == "date" and isinstance(produced.value, date) and produced.value == expected
    if xlsx and cell != "number":
        return False  # a number cell where the profile writes text
    mine = produced.value if isinstance(produced.value, Decimal) else _number(produced.text)
    theirs = _number(expected)
    return mine is not None and theirs is not None and mine == theirs


def _shown(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat() if isinstance(value, date) else str(value)


def compare(profile: Profile, produced: list[list[Cell]], accepted: bytes) -> Comparison:
    """How the engine's rows for some bills differ from the file the books accepted for
    them. Raises `SampleUnreadable`."""
    rows = read_rows(profile, accepted)
    xlsx = profile.file_type == "xlsx"
    names = headers(profile)
    width = len(profile.columns)
    diffs: list[CellDiff] = []
    cells = equal = 0

    def differs(row: int, column: str, theirs: Any, mine: str) -> None:
        if len(diffs) < MAX_DIFFS:
            diffs.append(CellDiff(row, column, _shown(theirs), mine))

    first = 1
    if names is not None:
        first = 2
        found = rows[0] if rows else []
        for at, name in enumerate(names):
            cells += 1
            theirs = found[at] if at < len(found) else None
            if _same(theirs, Cell(name), xlsx):
                equal += 1
            else:
                differs(1, name, theirs, _written(Cell(name), xlsx))
        rows = rows[1:]
    label = names or [f"column {n}" for n in range(1, width + 1)]
    for index in range(max(len(rows), len(produced))):
        theirs_row = rows[index] if index < len(rows) else []
        mine_row = produced[index] if index < len(produced) else []
        for at in range(width):
            cells += 1
            theirs = theirs_row[at] if at < len(theirs_row) else None
            mine = mine_row[at] if at < len(mine_row) else None
            kind = profile.columns[at].cell
            if mine is not None and _same(theirs, mine, xlsx, kind):
                equal += 1
            else:
                differs(
                    index + first, label[at], theirs, "" if mine is None else _written(mine, xlsx)
                )
        for at in range(width, len(theirs_row)):  # cells past the profile's columns
            if theirs_row[at] not in (None, ""):
                cells += 1
                differs(index + first, f"column {at + 1}", theirs_row[at], "")
    return Comparison(len(rows), len(produced), cells, equal, tuple(diffs))
