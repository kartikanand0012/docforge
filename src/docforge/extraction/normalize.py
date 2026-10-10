"""Printed strings to typed values, in code.

The model copies text; this module decides what the text means. A string that cannot be
read becomes a null value with an issue, never a guess.
"""

import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from docforge.extraction.schema import (
    Extracted,
    InvoiceExtraction,
    Issue,
    LineExtraction,
    PartyExtraction,
    RawField,
    RawInvoice,
    RawLine,
    RawParty,
    RawTotals,
    TotalsExtraction,
)
from docforge.parsing.base import ParsedDocument

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# re.ASCII throughout: `\d` must not match digits of other scripts, which int() would accept.
# The backreference makes both separators of a date the same character.
_DAY_MONTHNAME_YEAR = re.compile(r"(\d{1,2})([-/ ])([A-Za-z]{3})\2(\d{4})", re.ASCII)
_DAY_MONTH_YEAR = re.compile(r"(\d{1,2})([-/])(\d{1,2})\2(\d{4})", re.ASCII)
_YEAR_MONTH_DAY = re.compile(r"(\d{4})-(\d{2})-(\d{2})", re.ASCII)
_MONTH_YEAR = re.compile(r"(\d{1,2})[-/](\d{2}|\d{4})", re.ASCII)
# Plain digits, or commas in Western (1,234,567) or Indian (12,34,567) groups. "12,50" is
# neither, so it is rejected rather than read as 1250.
_DIGITS = r"(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})"
# A count may be printed to two places ("7.00"): a fraction of zeros is still a whole count.
# Any other fraction ("7.5") is not a count, and is not rounded into one.
_INTEGER = re.compile(rf"({_DIGITS})(?:\.0+)?", re.ASCII)
_NUMBER = re.compile(rf"-?{_DIGITS}(?:\.\d+)?", re.ASCII)
# A currency mark may lead and a percent sign may trail; neither may sit inside the digits.
_AFFIXES = re.compile(r"^(?:rs\.?|inr|₹)\s*|\s*%$", re.IGNORECASE)
_STATE_THEN_CODE = re.compile(r"(.+?)\s*\((\d{2})\)", re.ASCII)
_CODE_THEN_STATE = re.compile(r"(\d{2})\s*-\s*(.+)", re.ASCII)
_STATE_DASH_CODE = re.compile(r"(.+?)\s*-\s*(\d{2})", re.ASCII)
_CODE_ONLY = re.compile(r"\d{2}", re.ASCII)
_EARLIEST_YEAR = 1900


def clean_text(raw: str) -> str | None:
    """Collapse whitespace; an empty string means the value is missing."""
    return " ".join(raw.split()) or None


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(raw: str) -> date | None:
    """`02-Sep-2026`, `05/06/2026` (day first, as Indian documents print it) or ISO."""
    text = raw.strip()
    if match := _DAY_MONTHNAME_YEAR.fullmatch(text):
        name = match[3].lower()
        if name not in _MONTHS:
            return None
        return _date(int(match[4]), _MONTHS.index(name) + 1, int(match[1]))
    if match := _DAY_MONTH_YEAR.fullmatch(text):
        return _date(int(match[4]), int(match[3]), int(match[1]))
    if match := _YEAR_MONTH_DAY.fullmatch(text):
        return _date(int(match[1]), int(match[2]), int(match[3]))
    return None


def parse_month(raw: str) -> str | None:
    """`06/28` or `06/2028` to `2028-06`. Two-digit years are read as 20YY."""
    match = _MONTH_YEAR.fullmatch(raw.strip())
    if not match:
        return None
    month = int(match[1])
    year = 2000 + int(match[2]) if len(match[2]) == 2 else int(match[2])
    if not 1 <= month <= 12 or year < _EARLIEST_YEAR:
        return None
    return f"{year:04d}-{month:02d}"


def parse_decimal(raw: str) -> Decimal | None:
    """A printed amount or rate. Keeps the printed precision; drops a currency mark and commas."""
    text = _AFFIXES.sub("", raw.strip())
    return Decimal(text.replace(",", "")) if _NUMBER.fullmatch(text) else None


def parse_int(raw: str) -> int | None:
    """A printed count: `7`, `1,200` or `7.00`. `7.5` cannot be read as one."""
    match = _INTEGER.fullmatch(raw.strip())
    return int(match[1].replace(",", "")) if match else None


def parse_place_of_supply(raw: str) -> tuple[str | None, str | None]:
    """`Gujarat (24)`, `24-Gujarat` or `Gujarat - 24` to (state, code); either may be None."""
    text = raw.strip()
    if _CODE_ONLY.fullmatch(text):
        return None, text
    if match := _STATE_THEN_CODE.fullmatch(text):
        return match[1], match[2]
    if match := _CODE_THEN_STATE.fullmatch(text):
        return match[2], match[1]
    if match := _STATE_DASH_CODE.fullmatch(text):
        return match[1], match[2]
    return text, None


def product_name(position: int) -> Callable[[str], str | None]:
    """Parser for the product of line `position` (1-based).

    Parsers often merge the serial-number column into the product cell ("3 Cetirizine
    Tablets"), and the model may copy both. A leading number equal to the line's own
    position is dropped; any other leading number is part of the name.
    """
    prefix = f"{position} "

    def parse(raw: str) -> str | None:
        text = clean_text(raw)
        if text and text.startswith(prefix) and len(text) > len(prefix):
            return text[len(prefix) :]
        return text

    return parse


class Normalizer:
    """Converts raw fields for one document, collecting issues as it goes."""

    def __init__(self, parsed: ParsedDocument) -> None:
        self._known = {block.id for block in parsed.blocks}
        self.issues: list[Issue] = []

    def _citations(self, path: str, raw: RawField) -> tuple[str, ...]:
        unknown = [block_id for block_id in raw.block_ids if block_id not in self._known]
        if unknown:
            self.issues.append(
                Issue(
                    path=path,
                    code="unknown_block",
                    message=f"cited blocks that do not exist: {', '.join(unknown)}",
                )
            )
        return tuple(block_id for block_id in raw.block_ids if block_id in self._known)

    def field[T](self, path: str, raw: RawField, parse: Callable[[str], T | None]) -> Extracted[T]:
        block_ids = self._citations(path, raw)
        text = clean_text(raw.text) if raw.text is not None else None
        if text is None:
            return Extracted(value=None, raw=None, block_ids=block_ids)
        value = parse(text)
        if value is None:
            self.issues.append(
                Issue(path=path, code="unparseable", message=f"could not read {text!r}")
            )
        return Extracted(value=value, raw=text, block_ids=block_ids)

    def party(self, path: str, raw: RawParty) -> PartyExtraction:
        return PartyExtraction(
            name=self.field(f"{path}.name", raw.name, clean_text),
            address=self.field(f"{path}.address", raw.address, clean_text),
            gstin=self.field(f"{path}.gstin", raw.gstin, clean_text),
            drug_licence_nos=tuple(
                self.field(f"{path}.drug_licence_nos[{index}]", licence, clean_text)
                for index, licence in enumerate(raw.drug_licence_nos)
            ),
        )

    def line(self, path: str, raw: RawLine, position: int) -> LineExtraction:
        return LineExtraction(
            product_name=self.field(
                f"{path}.product_name", raw.product_name, product_name(position)
            ),
            pack=self.field(f"{path}.pack", raw.pack, clean_text),
            hsn=self.field(f"{path}.hsn", raw.hsn, clean_text),
            batch_no=self.field(f"{path}.batch_no", raw.batch_no, clean_text),
            mfg=self.field(f"{path}.mfg", raw.mfg, parse_month),
            expiry=self.field(f"{path}.expiry", raw.expiry, parse_month),
            qty=self.field(f"{path}.qty", raw.qty, parse_int),
            free_qty=self.field(f"{path}.free_qty", raw.free_qty, parse_int),
            mrp=self.field(f"{path}.mrp", raw.mrp, parse_decimal),
            ptr=self.field(f"{path}.ptr", raw.ptr, parse_decimal),
            discount_pct=self.field(f"{path}.discount_pct", raw.discount_pct, parse_decimal),
            taxable_value=self.field(f"{path}.taxable_value", raw.taxable_value, parse_decimal),
            gst_rate=self.field(f"{path}.gst_rate", raw.gst_rate, parse_decimal),
            amount=self.field(f"{path}.amount", raw.amount, parse_decimal),
        )

    def totals(self, raw: RawTotals) -> TotalsExtraction:
        return TotalsExtraction(
            taxable_value=self.field("totals.taxable_value", raw.taxable_value, parse_decimal),
            cgst=self.field("totals.cgst", raw.cgst, parse_decimal),
            sgst=self.field("totals.sgst", raw.sgst, parse_decimal),
            igst=self.field("totals.igst", raw.igst, parse_decimal),
            round_off=self.field("totals.round_off", raw.round_off, parse_decimal),
            grand_total=self.field("totals.grand_total", raw.grand_total, parse_decimal),
        )


def normalize_invoice(raw: RawInvoice, parsed: ParsedDocument) -> InvoiceExtraction:
    """Convert the model's printed strings to typed values and check its citations exist."""
    normalizer = Normalizer(parsed)
    if not raw.lines:
        normalizer.issues.append(
            Issue(path="lines", code="no_line_items", message="no line items were extracted")
        )
    place = normalizer.field("place_of_supply", raw.place_of_supply, clean_text)
    state, code = parse_place_of_supply(place.value) if place.value else (None, None)
    return InvoiceExtraction(
        invoice_no=normalizer.field("invoice_no", raw.invoice_no, clean_text),
        invoice_date=normalizer.field("invoice_date", raw.invoice_date, parse_date),
        po_no=normalizer.field("po_no", raw.po_no, clean_text),
        po_date=normalizer.field("po_date", raw.po_date, parse_date),
        place_of_supply=Extracted(value=state, raw=place.raw, block_ids=place.block_ids),
        place_of_supply_code=Extracted(value=code, raw=place.raw, block_ids=place.block_ids),
        seller=normalizer.party("seller", raw.seller),
        buyer=normalizer.party("buyer", raw.buyer),
        lines=tuple(
            normalizer.line(f"lines[{index}]", line, index + 1)
            for index, line in enumerate(raw.lines)
        ),
        totals=normalizer.totals(raw.totals),
        issues=tuple(normalizer.issues),
    )
