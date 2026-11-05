"""Reading a GSTR-2B download, to set beside the purchase register (V2 P4).

A comparison aid only: it reads a file the user downloaded from the GST portal; it never
fetches, files or accepts anything on the portal.

Two forms are read, B2B invoices only (amendments, credit and debit notes and imports are
other tables, and are left out):

- **Excel:** the sheet named "B2B". Its header is found by name in the first rows - "GSTIN
  of supplier", "Invoice number", "Invoice Date", "Invoice Value", "Taxable Value",
  "Integrated Tax", "Central Tax", "State/UT Tax", "Cess" - in one row or two (a group label
  above its sub-headers), so the columns' order does not matter. An invoice can take a row per
  tax rate; its rows are added up.
- **JSON:** `data.docdata.b2b[]` (or `docdata.b2b[]`), each supplier `ctin` with its `inv[]`
  (`inum`, `dt` as DD-MM-YYYY, `val`, and `items[]` of `txval`, `igst`, `cgst`, `sgst`, `cess`;
  or those amounts on the invoice itself).

The layout was taken from two independent open-source readers of the portal's downloads
(GSTN publishes no schema); a real 2B download is the test still to come
(docs/plans/v2-p4-purchase-register.md). Amounts are read as exact decimals.
"""

import io
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from docforge.profiles.compare import SampleUnreadable, check_workbook
from docforge.trust.duplicates import text_key

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 20_000
_HEADER_ROWS = 12  # the header is looked for in the first rows only
_EXCEL_EPOCH = date(1899, 12, 30)
_ZERO = Decimal("0")
_AMOUNTS = ("taxable_value", "igst", "cgst", "sgst", "cess")


class TwoBInvalid(ValueError):
    """The file cannot be read as a GSTR-2B download, said in words."""


@dataclass
class TwoBInvoice:
    """One B2B invoice as GSTR-2B shows it (its rate rows added up)."""

    supplier_gstin: str
    trade_name: str | None
    invoice_no: str
    invoice_date: date | None
    invoice_value: Decimal | None
    taxable_value: Decimal = _ZERO
    igst: Decimal = _ZERO
    cgst: Decimal = _ZERO
    sgst: Decimal = _ZERO
    cess: Decimal = _ZERO
    rows: int = 0
    keys: tuple[str, str] = field(default=("", ""), repr=False)  # GSTIN and number, compared

    @property
    def total_tax(self) -> Decimal:
        return self.igst + self.cgst + self.sgst


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value).replace(",", "").replace("₹", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 < value < 100_000:
        return _EXCEL_EPOCH + timedelta(days=int(value))  # an Excel serial date
    text = str(value or "").strip()
    for shape in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, shape).date()
        except ValueError:
            continue
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # an invoice number Excel held as a number
    text = " ".join(str(value).split())
    return text or None


class _Invoices:
    """Invoices by supplier GSTIN and number (compared as duplicates are), rows added up."""

    def __init__(self) -> None:
        self.found: dict[tuple[str, str], TwoBInvoice] = {}
        self.rows = 0

    def add(
        self, gstin: Any, name: Any, number: Any, when: Any, value: Any,
        amounts: dict[str, Any],
    ) -> None:  # fmt: skip
        self.rows += 1
        if self.rows > MAX_ROWS:
            raise TwoBInvalid(
                f"The file has more than {MAX_ROWS:,} invoice rows: download one month's "
                "GSTR-2B, or split it by supplier, and upload each part."
            )
        supplier, invoice = _text(gstin), _text(number)
        gkey, nkey = text_key(supplier), text_key(invoice)
        if not supplier or not invoice or not gkey or not nkey:
            return  # a blank, sub-header or total row
        key = (gkey, nkey)
        entry = self.found.get(key)
        if entry is None:
            entry = TwoBInvoice(
                supplier_gstin=supplier.upper(), trade_name=_text(name), invoice_no=invoice,
                invoice_date=_date(when), invoice_value=_decimal(value), keys=key,
            )  # fmt: skip
            self.found[key] = entry
        elif entry.invoice_value is None:
            entry.invoice_value = _decimal(value)
        entry.rows += 1
        for head in _AMOUNTS:
            amount = _decimal(amounts.get(head))
            if amount is not None:
                setattr(entry, head, getattr(entry, head) + amount)


# Excel: each column found by words in its header (lower case, spaces collapsed).
_COLUMNS = {
    "gstin": ("gstin of supplier",),
    "name": ("trade/legal name", "trade name", "legal name"),
    "number": ("invoice number", "invoice no"),
    "date": ("invoice date",),
    "value": ("invoice value",),
    "taxable_value": ("taxable value",),
    "igst": ("integrated tax",),
    "cgst": ("central tax",),
    "sgst": ("state/ut tax", "state tax"),
    "cess": ("cess",),
}
_NEEDED = ("gstin", "number", "taxable_value")


def _label(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _columns(rows: list[list[Any]]) -> tuple[dict[str, int], int] | None:
    """The column of each field, and the first data row, from a one- or two-row header."""
    for at, row in enumerate(rows[:_HEADER_ROWS]):
        labels = [_label(v) for v in row]
        if not any("gstin of supplier" in label for label in labels):
            continue
        below = [_label(v) for v in rows[at + 1]] if at + 1 < len(rows) else []
        found: dict[str, int] = {}
        for index in range(max(len(labels), len(below))):
            texts = [t for t in (labels[index] if index < len(labels) else "",
                                 below[index] if index < len(below) else "") if t]  # fmt: skip
            for name, words in _COLUMNS.items():
                if name not in found and any(w in t for t in texts for w in words):
                    found[name] = index
                    break
        if all(name in found for name in _NEEDED):
            return found, at + 1
    return None


def _excel(data: bytes) -> _Invoices:
    from openpyxl import load_workbook

    try:
        check_workbook(data)
    except SampleUnreadable as error:
        raise TwoBInvalid(str(error)) from error
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as error:  # a bad zip, or not a workbook
        raise TwoBInvalid("The file is not a workbook Excel can open.") from error
    try:
        name = next((n for n in workbook.sheetnames if n.strip().casefold() == "b2b"), None)
        if name is None:
            raise TwoBInvalid(
                "The workbook has no B2B sheet: upload the GSTR-2B Excel as the GST portal "
                "gives it."
            )
        found_rows = workbook[name].iter_rows(
            max_row=MAX_ROWS + _HEADER_ROWS + 2, max_col=40, values_only=True
        )
        rows = [list(r) for r in found_rows]
    finally:
        workbook.close()
    found = _columns(rows)
    if found is None:
        raise TwoBInvalid(
            "The B2B sheet has no header with 'GSTIN of supplier', 'Invoice number' and "
            "'Taxable Value': upload the GSTR-2B Excel as the GST portal gives it."
        )
    columns, first = found
    invoices = _Invoices()

    def cell(row: list[Any], name: str) -> Any:
        index = columns.get(name)
        return row[index] if index is not None and index < len(row) else None

    for row in rows[first:]:
        invoices.add(
            cell(row, "gstin"), cell(row, "name"), cell(row, "number"), cell(row, "date"),
            cell(row, "value"), {head: cell(row, head) for head in _AMOUNTS},
        )  # fmt: skip
    return invoices


def _suppliers(document: Any) -> Iterable[Any]:
    data = document.get("data", document) if isinstance(document, dict) else None
    docdata = data.get("docdata", data) if isinstance(data, dict) else None
    b2b = docdata.get("b2b") if isinstance(docdata, dict) else None
    if not isinstance(b2b, list):
        raise TwoBInvalid(
            "The JSON has no B2B invoices (data.docdata.b2b): upload the GSTR-2B JSON as the "
            "GST portal gives it."
        )
    return b2b


def _json(data: bytes) -> _Invoices:
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TwoBInvalid("The file is neither a GSTR-2B Excel nor its JSON.") from error
    invoices = _Invoices()
    for supplier in _suppliers(document):
        if not isinstance(supplier, dict):
            continue
        for invoice in supplier.get("inv") or ():
            if not isinstance(invoice, dict):
                continue
            items = [i for i in invoice.get("items") or () if isinstance(i, dict)] or [invoice]
            for item in items:
                invoices.add(
                    supplier.get("ctin"), supplier.get("trdnm"), invoice.get("inum"),
                    invoice.get("dt"), invoice.get("val"),
                    {"taxable_value": item.get("txval"), "igst": item.get("igst"),
                     "cgst": item.get("cgst"), "sgst": item.get("sgst"),
                     "cess": item.get("cess")},
                )  # fmt: skip
    return invoices


def read_2b(data: bytes) -> list[TwoBInvoice]:
    """The B2B invoices of a GSTR-2B Excel or JSON download. Raises `TwoBInvalid`."""
    if len(data) > MAX_BYTES:
        raise TwoBInvalid("The file is larger than 10 MB: download one month's GSTR-2B.")
    if not data.strip():
        raise TwoBInvalid("The file is empty.")
    invoices = _excel(data) if data.startswith(b"PK\x03\x04") else _json(data)
    return list(invoices.found.values())
