"""A purchase-import file layout, held as data (V2 P3).

A profile is an ordered list of columns. Each column names its header as the sample file
prints it, where its value comes from, and how it is written:

- `invoice`: a field of the bill (its number, date, totals);
- `line`: a field of the bill line (batch, expiry, quantity, free quantity, rate);
- `mapping`: a name from the workspace's mapping master (V2 P1) - the books' party, item,
  purchase ledger, tax treatment, or the preferred HSN;
- `computed`: worked out from the bill (the CGST / SGST / IGST split of a line, a free row's
  kind);
- `constant`: the same text on every row; `blank`: an empty cell.

The file is one row per bill line (batch-wise) or one row per bill, as CSV or XLSX. Free
(scheme) quantity is either its own column or its own row - never added to the billed
quantity, never dropped. A per-line profile must carry the mapped item, the batch, the
expiry and the free quantity, so no line goes into the books without them.

No layout here is a Marg layout. A workspace's profile is made from an import file its own
books accepted; the one built in (`sample_profile`) is labelled SAMPLE and is made-up.
"""

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Source = Literal["invoice", "line", "mapping", "computed", "constant", "blank"]
Kind = Literal["text", "date", "month", "money", "number", "int"]
Layout = Literal["per_line", "per_invoice"]
FileType = Literal["csv", "xlsx"]

# Each field, the kind of value it holds, and whether it is the bill's or a line's.
INVOICE_FIELDS: dict[str, Kind] = {
    "invoice_no": "text",
    "invoice_date": "date",
    "po_no": "text",
    "po_date": "date",
    "place_of_supply_code": "text",
    "seller_gstin": "text",
    "seller_name": "text",  # as printed: the books' name is mapping.party
    "buyer_gstin": "text",
    "taxable_value": "money",
    "cgst": "money",
    "sgst": "money",
    "igst": "money",
    "round_off": "money",
    "grand_total": "money",
}
LINE_FIELDS: dict[str, Kind] = {
    "line_no": "int",
    "product_name": "text",  # as printed: the books' name is mapping.item
    "pack": "text",
    "hsn": "text",
    "batch_no": "text",
    "mfg": "month",
    "expiry": "month",
    "qty": "int",
    "free_qty": "int",
    "mrp": "money",
    "ptr": "money",
    "discount_pct": "number",
    "taxable_value": "money",
    "gst_rate": "number",
    "amount": "money",
}
MAPPING_INVOICE_FIELDS: dict[str, Kind] = {
    "party": "text",
    "purchase_ledger": "text",
    "tax_treatment": "text",
}
MAPPING_LINE_FIELDS: dict[str, Kind] = {
    "item": "text",
    "hsn": "text",  # the preferred HSN, or else the printed one
}
COMPUTED_INVOICE_FIELDS: dict[str, Kind] = {
    "supply_type": "text",  # intra (CGST + SGST) or inter (IGST)
    "tax_total": "money",
    "line_count": "int",
}
COMPUTED_LINE_FIELDS: dict[str, Kind] = {
    "cgst_rate": "number",
    "sgst_rate": "number",
    "igst_rate": "number",
    "cgst": "money",
    "sgst": "money",
    "igst": "money",
    "tax": "money",
    "row_kind": "text",  # billed, or free for a free-quantity row
}


def fields_for(source: Source, layout: Layout) -> dict[str, Kind]:
    """The fields a column of `source` may name in a file of `layout`."""
    per_line = layout == "per_line"
    if source == "invoice":
        return INVOICE_FIELDS
    if source == "line":
        return LINE_FIELDS if per_line else {}
    if source == "mapping":
        return {**MAPPING_INVOICE_FIELDS, **(MAPPING_LINE_FIELDS if per_line else {})}
    if source == "computed":
        return {**COMPUTED_INVOICE_FIELDS, **(COMPUTED_LINE_FIELDS if per_line else {})}
    return {}


def kind_of(source: Source, field: str | None) -> Kind:
    if field is None:
        return "text"
    return fields_for(source, "per_line").get(field, "text")


_DATE_TOKEN = re.compile(r"YYYY|YY|MMM|MM|DD|[^A-Za-z]")


def date_tokens(pattern: str) -> list[str]:
    """`DD-MM-YYYY` as its parts; raises ValueError for a letter that is no date part."""
    tokens, at = [], 0
    while at < len(pattern):
        found = _DATE_TOKEN.match(pattern, at)
        if found is None:
            raise ValueError(
                f"the date pattern {pattern!r} can use only DD, MM, MMM, YY, YYYY and "
                "separators such as - / ."
            )
        tokens.append(found.group())
        at = found.end()
    return tokens


class Format(BaseModel):
    """How a column's value is written."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: str | None = Field(default=None, max_length=20)  # e.g. DD-MM-YYYY, MM/YY
    month_day: Literal["first", "last"] = "first"  # for an expiry month written with DD
    decimals: int | None = Field(default=None, ge=0, le=6)
    codes: dict[str, str] = Field(default_factory=dict, max_length=100)  # value -> code
    upper: bool = False
    empty: str = Field(default="", max_length=20)  # what a missing value is written as

    @model_validator(mode="after")
    def _date(self) -> Self:
        if self.date is not None:
            tokens = date_tokens(self.date)
            if not any(t in ("MM", "MMM") for t in tokens):
                raise ValueError(f"the date pattern {self.date!r} has no month (MM or MMM)")
        return self


class Column(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    header: str = Field(max_length=100)  # as the sample prints it
    source: Source
    field: str | None = Field(default=None, max_length=40)
    value: str | None = Field(default=None, max_length=200)  # a constant's text
    format: Format = Field(default_factory=Format)
    cell: Literal["text", "number", "date"] = "text"  # how an XLSX cell holds it
    required: bool = False  # a line without it is refused before export, by line

    @property
    def kind(self) -> Kind:
        return kind_of(self.source, self.field)

    def names(self, source: Source, field: str) -> bool:
        return self.source == source and self.field == field


MAX_COLUMNS = 80


class Profile(BaseModel):
    """An import file's layout. Raises ValueError, in words, for one that cannot be made."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_type: FileType = "csv"
    layout: Layout = "per_line"
    delimiter: Literal[",", ";", "\t", "|"] = ","
    quoting: Literal["minimal", "all"] = "minimal"
    header_row: bool = True
    line_end: Literal["crlf", "lf"] = "crlf"
    encoding: Literal["utf-8", "utf-8-sig"] = "utf-8"
    sheet_name: str = Field(default="Sheet1", min_length=1, max_length=31)
    free_qty: Literal["column", "row"] = "column"  # its own column, or its own row
    columns: tuple[Column, ...] = Field(min_length=1, max_length=MAX_COLUMNS)

    @model_validator(mode="after")
    def _columns(self) -> Self:
        for number, column in enumerate(self.columns, start=1):
            where = f"Column {number} ({column.header or 'no header'})"
            if column.source == "constant":
                if column.value is None:
                    raise ValueError(f"{where} is a constant with no text")
            elif column.source == "blank":
                pass
            elif column.field not in fields_for(column.source, self.layout):
                raise ValueError(
                    f"{where} takes {column.source}.{column.field}, which a "
                    f"{'line' if self.layout == 'per_line' else 'bill'} row does not have"
                )
            if column.cell == "date" and column.kind not in ("date", "month"):
                raise ValueError(f"{where} is a date cell for a value that is not a date")
        headers = [c.header.strip().casefold() for c in self.columns]
        if self.header_row and len(set(headers)) != len(headers):
            raise ValueError("Two columns have the same header")
        if self.layout == "per_line":
            self._carries("mapping", "item", "the item as the books name it")
            self._carries("line", "batch_no", "the batch")
            self._carries("line", "expiry", "the expiry")
            if self.free_qty == "column":
                self._carries("line", "free_qty", "the free quantity")
            else:
                self._carries("line", "qty", "the quantity (a free row carries its free quantity)")
                self._carries("computed", "row_kind", "which rows are free")
        return self

    def _carries(self, source: Source, field: str, what: str) -> None:
        if not any(c.names(source, field) for c in self.columns):
            raise ValueError(f"A line-by-line file must carry {what} ({source}.{field})")


# The built-in SAMPLE: made-up headers over made-up data, to try the engine with. It is no
# Marg layout; a workspace's own profile is made from an import file its books accepted.
SAMPLE_NAME = "SAMPLE (made-up layout, not a Marg format)"


def sample_profile() -> Profile:
    def col(header: str, source: Source, field: str | None = None, **rest: object) -> Column:
        return Column.model_validate({"header": header, "source": source, "field": field, **rest})

    money = {"decimals": 2}
    return Profile(
        file_type="csv",
        layout="per_line",
        delimiter=",",
        header_row=True,
        line_end="crlf",
        free_qty="column",
        columns=(
            col("Bill No", "invoice", "invoice_no"),
            col("Bill Date", "invoice", "invoice_date", format={"date": "DD-MM-YYYY"}),
            col("Supplier", "mapping", "party"),
            col("Supplier GSTIN", "invoice", "seller_gstin"),
            col("Item", "mapping", "item"),
            col("Pack", "line", "pack"),
            col("Batch", "line", "batch_no", required=True),
            col("Mfg", "line", "mfg", format={"date": "MM/YY"}),
            col("Expiry", "line", "expiry", format={"date": "MM/YY"}, required=True),
            col("Qty", "line", "qty"),
            col("Free", "line", "free_qty", format={"empty": "0"}),
            col("MRP", "line", "mrp", format=money),
            col("Rate", "line", "ptr", format=money),
            col("Disc %", "line", "discount_pct", format=money),
            col("HSN", "mapping", "hsn"),
            col("GST %", "line", "gst_rate", format=money),
            col("CGST", "computed", "cgst", format=money),
            col("SGST", "computed", "sgst", format=money),
            col("IGST", "computed", "igst", format=money),
            col("Amount", "line", "amount", format=money),
            col("Bill Total", "invoice", "grand_total", format=money),
        ),
    )
