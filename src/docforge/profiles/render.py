"""Bills, booked under the workspace's names, written in a profile's layout (V2 P3).

`table` works out every cell (its text, and for an XLSX number or date cell, its value);
`render` writes them as the profile's file. Both are pure: the mappings are loaded before,
in bulk, so a thousand bills cost no query per row. The same bills and profile always give
the same bytes - an XLSX file's timestamps are fixed - so a past file can be made again.

Values are the signed record's, as exact decimals. A line's CGST / SGST / IGST is its
taxable value at its rate: two halves rounded separately within a state (the place of supply
is the seller's state), one whole tax across states; the bill's printed totals were checked
against these before export (`docforge.preflight`).

A free (scheme) quantity is its own column, or with `free_qty: "row"` its own row after the
line: that row's quantity is the free quantity and its money is nil, so nothing is billed
twice and nothing free is lost.
"""

import calendar
import io
import re
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from docforge.csvsafe import write_table
from docforge.extraction.schema import LineExtraction
from docforge.mappings.service import Bill, ItemMappingView, SupplierMappingView
from docforge.profiles.spec import Column, Profile, date_tokens

Value = str | int | Decimal | date | None

_CENT = Decimal("0.01")
_NIL = Decimal("0.00")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FIXED_TIME = (2000, 1, 1, 0, 0, 0)
_FIXED_STAMP = b"2000-01-01T00:00:00Z"
_STAMP = re.compile(rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:)")


@dataclass(frozen=True)
class Booked:
    """A bill with its party and each line's item as the books name them."""

    bill: Bill
    party: SupplierMappingView
    items: tuple[ItemMappingView, ...]  # one per line, in the bill's order


@dataclass(frozen=True)
class Cell:
    text: str  # as written to a CSV, or an XLSX text cell
    value: Decimal | date | None = None  # for an XLSX number or date cell


def money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def intra_state(bill: Bill) -> bool | None:
    """Within one state (CGST + SGST) when the place of supply is the seller's state, as the
    GST law has it (IGST Act, sections 7 and 8); across states (IGST) otherwise; None when
    either is not known. On a purchase the place of supply is nearly always the buyer's
    own state, so it is the seller's state that decides."""
    code = bill.invoice.place_of_supply_code.value
    seller = bill.invoice.seller.gstin.value
    if not code or not seller:
        return None
    return code == seller.strip().upper()[:2]


def line_tax(
    taxable: Decimal | None, rate: Decimal | None, intra: bool | None
) -> dict[str, Decimal]:
    """A line's tax by head, as the bill was checked against; empty when not known."""
    if taxable is None or rate is None or intra is None:
        return {}
    if intra:
        half = money(taxable * rate / Decimal(200))
        return {"cgst": half, "sgst": half, "igst": _NIL}
    return {"cgst": _NIL, "sgst": _NIL, "igst": money(taxable * rate / Decimal(100))}


@dataclass(frozen=True)
class _Row:
    booked: Booked
    intra: bool | None
    free_rows: bool  # free quantity on rows of its own
    index: int | None = None  # the line's place on the bill, from 0
    free: bool = False  # this is the free-quantity row after a line

    @property
    def line(self) -> LineExtraction | None:
        return None if self.index is None else self.booked.bill.invoice.lines[self.index]


def _invoice_value(field: str, row: _Row) -> Value:
    invoice = row.booked.bill.invoice
    totals = invoice.totals
    plain: dict[str, Value] = {
        "invoice_no": invoice.invoice_no.value,
        "invoice_date": invoice.invoice_date.value,
        "po_no": invoice.po_no.value,
        "po_date": invoice.po_date.value,
        "place_of_supply_code": invoice.place_of_supply_code.value,
        "seller_gstin": invoice.seller.gstin.value,
        "seller_name": invoice.seller.name.value,
        "buyer_gstin": invoice.buyer.gstin.value,
        "taxable_value": totals.taxable_value.value,
        "cgst": totals.cgst.value,
        "sgst": totals.sgst.value,
        "igst": totals.igst.value,
        "round_off": totals.round_off.value,
        "grand_total": totals.grand_total.value,
    }
    return plain[field]


_FREE_ROW: dict[str, Value] = {  # the free row: the free quantity, at no price
    "free_qty": 0,
    "ptr": _NIL,
    "discount_pct": _NIL,
    "taxable_value": _NIL,
    "amount": _NIL,
}


def _line_value(field: str, row: _Row) -> Value:
    line = row.line
    if line is None or row.index is None:
        return None
    if row.free:
        if field == "qty":
            return line.free_qty.value
        if field in _FREE_ROW:
            return _FREE_ROW[field]
    elif field == "free_qty" and row.free_rows:
        return None  # on its own row instead, never twice
    if field == "line_no":
        return row.index + 1
    value: Value = getattr(line, field).value
    return value


def _mapping_value(field: str, row: _Row) -> Value:
    party = row.booked.party
    if field == "party":
        return party.name_for("marg")
    if field == "purchase_ledger":
        return party.purchase_ledger
    if field == "tax_treatment":
        return party.tax_treatment
    line = row.line
    if row.index is None or line is None:
        return None
    item = row.booked.items[row.index]
    if field == "item":
        return item.name_for("marg")
    return item.preferred_hsn or line.hsn.value  # hsn


def _computed_value(field: str, row: _Row) -> Value:
    invoice = row.booked.bill.invoice
    if field == "supply_type":
        return None if row.intra is None else ("intra" if row.intra else "inter")
    if field == "tax_total":
        heads = (invoice.totals.cgst.value, invoice.totals.sgst.value, invoice.totals.igst.value)
        if all(v is None for v in heads):
            return None
        return sum((v for v in heads if v is not None), Decimal(0))
    if field == "line_count":
        return len(invoice.lines)
    if field == "row_kind":
        return "free" if row.free else "billed"
    line = row.line
    if line is None:
        return None
    rate = line.gst_rate.value
    if field.endswith("_rate"):
        if rate is None or row.intra is None:
            return None
        if field == "igst_rate":
            return _NIL if row.intra else rate
        return rate / 2 if row.intra else _NIL
    if row.free:
        return _NIL
    tax = line_tax(line.taxable_value.value, rate, row.intra)
    if not tax:
        return None
    if field == "tax":
        return tax["cgst"] + tax["sgst"] + tax["igst"]
    return tax[field]


_READERS = {
    "invoice": _invoice_value,
    "line": _line_value,
    "mapping": _mapping_value,
    "computed": _computed_value,
}


def _value(column: Column, row: _Row) -> Value:
    if column.source == "constant":
        return column.value
    if column.source == "blank" or column.field is None:
        return None
    return _READERS[column.source](column.field, row)


def _month(value: str, day: str) -> date | None:
    try:
        year, month = int(value[:4]), int(value[5:7])
        last = calendar.monthrange(year, month)[1]
        return date(year, month, last if day == "last" else 1)
    except (ValueError, IndexError):
        return None


def _date_text(when: date, pattern: str) -> str:
    parts = {
        "YYYY": f"{when.year:04d}",
        "YY": f"{when.year % 100:02d}",
        "MMM": _MONTHS[when.month - 1],
        "MM": f"{when.month:02d}",
        "DD": f"{when.day:02d}",
    }
    return "".join(parts.get(token, token) for token in date_tokens(pattern))


def _number_text(value: Decimal | int, decimals: int | None) -> str:
    number = Decimal(value)
    if decimals is not None:
        with suppress(InvalidOperation):  # too many digits to round: written as it is
            number = number.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return format(number, "f")


def format_value(value: Value, column: Column) -> Cell:
    """`value` written as `column` says. A missing value is the column's `empty` text."""
    rule = column.format
    if value is None or value == "":
        return Cell(rule.empty)
    typed: Decimal | date | None = None
    if column.kind == "month" and isinstance(value, str):
        when = _month(value, rule.month_day)
        text = _date_text(when, rule.date) if when and rule.date else value
        typed = when
    elif isinstance(value, date):
        text = _date_text(value, rule.date) if rule.date else value.isoformat()
        typed = value
    elif isinstance(value, (Decimal, int)) and not isinstance(value, bool):
        text = _number_text(value, rule.decimals)
        typed = Decimal(text)
    else:
        text = str(value)
    written = rule.codes.get(text, text)
    if rule.upper:
        written = written.upper()
    if written != text:  # a code or upper case: the cell holds what is written, as written
        text, typed = written, None
    if column.cell == "number" and not isinstance(typed, Decimal):
        try:
            typed = Decimal(text)
        except InvalidOperation:
            typed = None
    return Cell(text, typed)


XLSX_MAX_TEXT = 32_767
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # what an Excel cell cannot hold


@dataclass(frozen=True)
class Unwritable:
    """A cell an Excel file cannot hold: a control character, or too long."""

    booked: Booked
    line: int | None  # the bill's line, from 1
    header: str


def unwritable(profile: Profile, booked: Sequence[Booked]) -> list[Unwritable]:
    """Every cell an XLSX file of `profile` could not hold (none for CSV)."""
    if profile.file_type != "xlsx":
        return []
    found = []
    for row, cells in _rows(profile, booked):
        for column, cell in zip(profile.columns, cells, strict=True):
            if len(cell.text) > XLSX_MAX_TEXT or _ILLEGAL.search(cell.text):
                line = None if row.index is None else row.index + 1
                found.append(Unwritable(row.booked, line, column.header or "(no header)"))
    return found


def _rows(profile: Profile, booked: Sequence[Booked]) -> Iterator[tuple[_Row, list[Cell]]]:
    free_rows = profile.free_qty == "row"
    for entry in booked:
        intra = intra_state(entry.bill)
        if profile.layout == "per_invoice":
            places = [_Row(entry, intra, free_rows)]
        else:
            places = []
            for index, line in enumerate(entry.bill.invoice.lines):
                places.append(_Row(entry, intra, free_rows, index))
                if free_rows and (line.free_qty.value or 0) > 0:
                    places.append(_Row(entry, intra, free_rows, index, free=True))
        for row in places:
            yield row, [format_value(_value(c, row), c) for c in profile.columns]


def table(profile: Profile, booked: Sequence[Booked]) -> list[list[Cell]]:
    """Every row of the file but the header, in order: each bill's lines (or the bill
    itself, per bill), in the order the bills are given."""
    return [cells for _, cells in _rows(profile, booked)]


def headers(profile: Profile) -> list[str] | None:
    return [c.header for c in profile.columns] if profile.header_row else None


def csv_text(profile: Profile, cells: list[list[Cell]]) -> str:
    return write_table(
        headers(profile),
        ([cell.text for cell in row] for row in cells),
        delimiter=profile.delimiter,
        quote_all=profile.quoting == "all",
        line_end="\r\n" if profile.line_end == "crlf" else "\n",
    )


def render(profile: Profile, booked: Sequence[Booked]) -> bytes:
    """The file, as the books import it."""
    cells = table(profile, booked)
    if profile.file_type == "xlsx":
        return _xlsx(profile, cells)
    return csv_text(profile, cells).encode(profile.encoding)


def _excel_format(pattern: str) -> str:
    return "".join(
        {"YYYY": "yyyy", "YY": "yy", "MMM": "mmm", "MM": "mm", "DD": "dd"}.get(t, t)
        for t in date_tokens(pattern)
    )


def _xlsx(profile: Profile, cells: list[list[Cell]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(profile.sheet_name)

    def text_cell(text: str) -> object:
        cell = WriteOnlyCell(sheet, value=text)
        cell.data_type = "s"  # text, even one that starts with "=": never a formula
        return cell

    names = headers(profile)
    if names is not None:
        sheet.append([text_cell(name) for name in names])
    for row in cells:
        out: list[object] = []
        for column, cell in zip(profile.columns, row, strict=True):
            if column.cell == "number" and isinstance(cell.value, Decimal):
                out.append(cell.value)
            elif column.cell == "date" and isinstance(cell.value, date):
                written = WriteOnlyCell(sheet, value=cell.value)
                written.number_format = _excel_format(column.format.date or "DD-MM-YYYY")
                out.append(written)
            elif cell.text == "":
                out.append(None)
            else:
                out.append(text_cell(cell.text))
        sheet.append(out)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return _fixed(buffer.getvalue())


def _fixed(data: bytes) -> bytes:
    """The same workbook with fixed times, so the same rows always make the same bytes."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as source,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            content = source.read(info)
            if info.filename == "docProps/core.xml":
                content = _STAMP.sub(rb"\g<1>" + _FIXED_STAMP + rb"\g<2>", content)
            fixed = zipfile.ZipInfo(info.filename, date_time=_FIXED_TIME)
            fixed.compress_type = zipfile.ZIP_DEFLATED
            fixed.external_attr = 0o600 << 16
            target.writestr(fixed, content)
    return out.getvalue()
