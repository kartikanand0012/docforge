"""Reading an import file the books accepted, to start a profile from (V2 P3).

`read_sample` works out the file's layout - CSV or XLSX, its delimiter, whether the first row
is a header - and, for each column, a few example values and an offered source: "Batch No"
is offered as the line's batch, "Exp" as its expiry, with the date pattern its examples show
("10/28" as MM/YY). Nothing is saved: a person checks each column and saves the profile, and
a column with no offer is left for them to choose.

Only the file's own text is read; the examples are shown back to the person who uploaded it
and are never logged or audited.
"""

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from docforge.profiles.compare import SampleUnreadable, check_workbook, read_rows
from docforge.profiles.spec import MAX_COLUMNS, Column, FileType, Format, Profile, Source

MAX_SAMPLE_BYTES = 2 * 1024 * 1024
EXAMPLES = 3
Delimiter = Literal[",", ";", "\t", "|"]
_DELIMITERS: tuple[Delimiter, ...] = (",", ";", "\t", "|")

# Headers as written in purchase files (lower case, letters and digits only, % as "pct"),
# and the source each is offered as. Offers only: a person confirms each before it is saved.
_OFFERS: dict[str, tuple[Source, str]] = {}
_OFFERED: tuple[tuple[str, tuple[Source, str]], ...] = (
    ("billno invoiceno invno billnumber invoicenumber voucherno vchno partybillno suppinvno "
     "supplierinvoiceno refno", ("invoice", "invoice_no")),
    ("billdate invoicedate invdate date vchdate voucherdate partybilldate",
     ("invoice", "invoice_date")),
    ("supplier suppliername party partyname vendor vendorname", ("mapping", "party")),
    ("gstin suppliergstin partygstin gstno gstnumber", ("invoice", "seller_gstin")),
    ("item itemname product productname description particulars", ("mapping", "item")),
    ("pack packing packsize", ("line", "pack")),
    ("batch batchno batchnumber", ("line", "batch_no")),
    ("mfg mfgdate mfd manufacturing mfgdt", ("line", "mfg")),
    ("exp expiry expdate expirydate expdt", ("line", "expiry")),
    ("qty quantity billedqty purqty", ("line", "qty")),
    ("free freeqty scheme schqty schemeqty bonus bonusqty", ("line", "free_qty")),
    ("mrp", ("line", "mrp")),
    ("rate ptr purrate purchaserate price", ("line", "ptr")),
    ("disc discpct discount discountpct dis dispct", ("line", "discount_pct")),
    ("hsn hsncode hsnsac", ("mapping", "hsn")),
    ("gst gstpct gstrate taxrate taxpct", ("line", "gst_rate")),
    ("cgst cgstamt cgstamount", ("computed", "cgst")),
    ("sgst sgstamt sgstamount", ("computed", "sgst")),
    ("igst igstamt igstamount", ("computed", "igst")),
    ("cgstpct cgstrate", ("computed", "cgst_rate")),
    ("sgstpct sgstrate", ("computed", "sgst_rate")),
    ("igstpct igstrate", ("computed", "igst_rate")),
    ("amount amt netamount netamt linetotal value", ("line", "amount")),
    ("taxable taxablevalue taxableamt taxableamount", ("line", "taxable_value")),
    ("billtotal billamount invoicetotal invoiceamount nettotal grandtotal",
     ("invoice", "grand_total")),
    ("roundoff rounding", ("invoice", "round_off")),
)  # fmt: skip
for _names, _source in _OFFERED:
    for _name in _names.split():
        _OFFERS[_name] = _source

_DATE_SHAPES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(shape), pattern)
    for shape, pattern in (
        (r"\d{2}-\d{2}-\d{4}", "DD-MM-YYYY"),
        (r"\d{2}/\d{2}/\d{4}", "DD/MM/YYYY"),
        (r"\d{2}\.\d{2}\.\d{4}", "DD.MM.YYYY"),
        (r"\d{4}-\d{2}-\d{2}", "YYYY-MM-DD"),
        (r"\d{2}-\d{2}-\d{2}", "DD-MM-YY"),
        (r"\d{2}/\d{2}/\d{2}", "DD/MM/YY"),
        (r"\d{2}/\d{4}", "MM/YYYY"),
        (r"\d{2}-\d{4}", "MM-YYYY"),
        (r"\d{2}/\d{2}", "MM/YY"),
        (r"\d{2}-\d{2}", "MM-YY"),
        (r"[A-Za-z]{3}-\d{2}", "MMM-YY"),
        (r"[A-Za-z]{3}-\d{4}", "MMM-YYYY"),
        (r"\d{8}", "YYYYMMDD"),
    )
)
_DATED = {"invoice_date", "po_date", "mfg", "expiry"}


@dataclass(frozen=True)
class SampleColumn:
    header: str  # "" when the file has no header row
    examples: tuple[str, ...]
    offer: Column | None  # what it looks like it holds; None when nothing is offered


@dataclass(frozen=True)
class SampleLayout:
    file_type: FileType
    delimiter: Delimiter
    header_row: bool
    encoding: Literal["utf-8", "utf-8-sig"]
    sheet_name: str
    rows: int  # data rows, without the header
    columns: tuple[SampleColumn, ...]


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.casefold().replace("%", "pct"))


def _shown(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat() if isinstance(value, date) else str(value)


def _numeric(text: str) -> bool:
    try:
        Decimal(text.replace(",", ""))
    except InvalidOperation:
        return False
    return bool(text.strip())


def _date_pattern(examples: Sequence[str]) -> str | None:
    for shape, pattern in _DATE_SHAPES:
        if examples and all(shape.fullmatch(e) for e in examples):
            return pattern
    return None


def _decimals(examples: Sequence[str]) -> int | None:
    places = {len(e.split(".")[1]) if "." in e else 0 for e in examples if _numeric(e)}
    return places.pop() if len(places) == 1 else None


def _offer(header: str, values: Sequence[Any]) -> Column | None:
    found = _OFFERS.get(_key(header))
    if found is None:
        return None
    source, field = found
    texts = [_shown(v) for v in values if _shown(v) != ""]
    rule: dict[str, Any] = {}
    cell = "text"
    if any(isinstance(v, (date, datetime)) for v in values):
        if field in _DATED:
            cell = "date"
            rule["date"] = "DD-MM-YYYY"
    elif field in _DATED:
        pattern = _date_pattern(texts)
        if pattern:
            rule["date"] = pattern
    elif texts and all(_numeric(t) for t in texts):
        if any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            cell = "number"
        decimals = _decimals(texts)
        if decimals is not None and any("." in t for t in texts):
            rule["decimals"] = decimals
    try:
        return Column(
            header=header[:100], source=source, field=field, format=Format(**rule), cell=cell
        )
    except ValueError:
        return None


def _delimiter(text: str) -> Delimiter:
    lines = [line for line in text.splitlines()[:20] if line.strip()]
    best: Delimiter = ","
    best_count = 0
    for candidate in _DELIMITERS:
        counts = {len(next(csv.reader([line], delimiter=candidate))) for line in lines}
        if len(counts) == 1 and (count := counts.pop()) > best_count:
            best, best_count = candidate, count
    return best


def _is_header(first: Sequence[Any], rest: Sequence[Sequence[Any]]) -> bool:
    texts = [_shown(v).strip() for v in first]
    if not texts or any(t == "" for t in texts) or len(set(texts)) != len(texts):
        return False
    if any(not isinstance(v, str) or _numeric(v) or _date_pattern([v.strip()]) for v in first):
        return False
    return any(_key(t) in _OFFERS for t in texts) or not rest


def read_sample(data: bytes) -> SampleLayout:
    """The layout of an import file, and an offered source for each column. Raises
    `SampleUnreadable`, in words."""
    if len(data) > MAX_SAMPLE_BYTES:
        raise SampleUnreadable("The file is larger than 2 MB: one bill or a few are enough.")
    if not data.strip():
        raise SampleUnreadable("The file is empty.")
    xlsx = data.startswith(b"PK\x03\x04")
    file_type: FileType = "xlsx" if xlsx else "csv"
    encoding: Literal["utf-8", "utf-8-sig"] = (
        "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
    )
    delimiter: Delimiter = ","
    sheet_name = "Sheet1"
    if xlsx:
        from openpyxl import load_workbook

        check_workbook(data)
        try:
            workbook = load_workbook(io.BytesIO(data), read_only=True)
            sheet_name = workbook.sheetnames[0]
            workbook.close()
        except Exception as error:  # a bad zip, or not a workbook
            raise SampleUnreadable("The file is not a workbook Excel can open.") from error
    else:
        try:
            delimiter = _delimiter(data.decode("utf-8-sig"))
        except csv.Error as error:
            raise SampleUnreadable(f"The file cannot be read as CSV: {error}.") from error
        except UnicodeDecodeError as error:
            raise SampleUnreadable(
                "The file is not UTF-8 text. Save it from Excel as CSV UTF-8, or upload the "
                "Excel file itself."
            ) from error
    probe = Profile(
        file_type=file_type,
        layout="per_invoice",
        delimiter=delimiter,
        sheet_name=sheet_name[:31],
        columns=(Column(header="probe", source="blank"),),
    )
    rows = read_rows(probe, data)
    if not rows:
        raise SampleUnreadable("The file has no rows.")
    width = max(len(row) for row in rows)
    if width > MAX_COLUMNS:
        raise SampleUnreadable(f"The file has more than {MAX_COLUMNS} columns.")
    header_row = _is_header(rows[0], rows[1:])
    body = rows[1:] if header_row else rows
    columns = []
    for at in range(width):
        header = _shown(rows[0][at]).strip() if header_row and at < len(rows[0]) else ""
        values = [row[at] for row in body if at < len(row) and _shown(row[at]) != ""]
        columns.append(
            SampleColumn(
                header=header,
                examples=tuple(_shown(v)[:60] for v in values[:EXAMPLES]),
                offer=_offer(header, values[:50]) if header else None,
            )
        )
    return SampleLayout(
        file_type=file_type,
        delimiter=delimiter,
        header_row=header_row,
        encoding=encoding,
        sheet_name=sheet_name[:31],
        rows=len(body),
        columns=tuple(columns),
    )
