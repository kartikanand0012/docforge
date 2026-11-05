"""Pre-flight: what a bill must hold before an import file books it (V2 P2, P3).

Shared by every import format (the Marg purchase import now, the Tally XML next), so a bill
is refused for the same reasons whichever books it goes to. Each failure names the bill
(its invoice number and file) and, for a line, the line number, and says what to correct;
a run with any failure makes no file at all - never a partial one.

A bill passes when:

- it has an invoice number, an invoice date and lines;
- its place of supply is a 2-digit state code, so its tax is known to be CGST + SGST (the
  place of supply is the seller's state) or IGST (it is not), and the tax heads it prints
  agree (the seller's GSTIN is checked with its mapping, `mappings.service.resolve`);
- its totals reconcile in exact decimals, as the review checks them (`totals.taxable_value`,
  `totals.tax`, `totals.grand_total`: each within 0.01);
- the tax at each line's GST rate, rounded line by line or rate by rate, is the tax it
  prints within 0.01;
- each line has its quantity, taxable value and GST rate; a medicine bill's lines each have
  a batch and an expiry; and every line has whatever else the import layout requires.
"""

import re
from collections.abc import Iterable, Sequence
from decimal import Decimal

from docforge.extraction.schema import InvoiceExtraction, LineExtraction
from docforge.gstin import is_valid_gstin
from docforge.mappings.service import Bill, BillProblem
from docforge.profiles.render import intra_state, line_tax, money
from docforge.trust.invoice_rules import (
    _medicine,  # the review's own test of a medicine bill
    totals_grand_total,
    totals_tax,
    totals_taxable_value,
)
from docforge.trust.rules import RuleResult

TOLERANCE = Decimal("0.01")
_STATE_CODE = re.compile(r"\d{2}")

# A line field an import layout may require, as a clerk would name it.
LINE_FIELD_NAMES = {
    "product_name": "product name",
    "pack": "pack",
    "hsn": "HSN",
    "batch_no": "batch number",
    "mfg": "manufacturing month",
    "expiry": "expiry",
    "qty": "quantity",
    "free_qty": "free quantity",
    "mrp": "MRP",
    "ptr": "rate (PTR)",
    "discount_pct": "discount",
    "taxable_value": "taxable value",
    "gst_rate": "GST rate",
    "amount": "amount",
}
_ALWAYS = ("qty", "taxable_value", "gst_rate")
_MEDICINE = ("batch_no", "expiry")


def _who(bill: Bill) -> str:
    number = bill.invoice.invoice_no.value
    return f"Invoice {number}" if number else "This bill"


def _fix(text: str) -> str:
    return f"{text}: correct it on the bill, then export again."


def _failed(results: Iterable[RuleResult]) -> list[RuleResult]:
    return [r for r in results if r.outcome == "failed"]


def _tax_printed(invoice: InvoiceExtraction) -> Decimal | None:
    totals = invoice.totals
    heads = (totals.cgst.value, totals.sgst.value, totals.igst.value)
    if any(v is not None for v in heads):
        return sum((v for v in heads if v is not None), Decimal(0))
    taxable, grand = totals.taxable_value.value, totals.grand_total.value
    if taxable is None or grand is None:
        return None
    return grand - (totals.round_off.value or Decimal(0)) - taxable  # one "GST n%" line


def _tax(taxable: Decimal, rate: Decimal, intra: bool) -> Decimal:
    return sum(line_tax(taxable, rate, intra).values(), Decimal(0))


def _tax_at_rates(lines: Sequence[LineExtraction], intra: bool) -> tuple[Decimal, ...] | None:
    """The tax at each line's rate, rounded line by line, and rate by rate."""
    by_line = Decimal(0)
    by_rate: dict[Decimal, Decimal] = {}
    for line in lines:
        taxable, rate = line.taxable_value.value, line.gst_rate.value
        if taxable is None or rate is None:
            return None
        by_line += _tax(taxable, rate, intra)
        by_rate[rate] = by_rate.get(rate, Decimal(0)) + taxable
    grouped = sum((_tax(t, r, intra) for r, t in by_rate.items()), Decimal(0))
    return by_line, grouped


def _bill(bill: Bill, required: Sequence[str]) -> list[BillProblem]:
    invoice = bill.invoice
    who = _who(bill)
    problems: list[BillProblem] = []

    def problem(text: str, line: int | None = None) -> None:
        problems.append(BillProblem(bill.document_id, bill.filename, _fix(text), line))

    if invoice.invoice_no.value is None:
        problem("This bill has no invoice number")
    if invoice.invoice_date.value is None:
        problem(f"{who} has no invoice date")
    if not invoice.lines:
        problem(f"{who} has no lines")
        return problems
    code = invoice.place_of_supply_code.value
    seller = invoice.seller.gstin.value
    if code is None or not _STATE_CODE.fullmatch(code):
        problem(f"{who} has no 2-digit place-of-supply state code, so its tax cannot be split")
    intra = intra_state(bill) if seller and is_valid_gstin(seller.strip().upper()) else None
    totals = invoice.totals
    if intra is True and totals.igst.value:
        problem(
            f"{who} is supplied within the seller's state ({code}) but prints IGST, where "
            "CGST + SGST is due"
        )
    if intra is False and (totals.cgst.value or totals.sgst.value):
        problem(
            f"{who} is supplied to state {code}, outside the seller's state, but prints "
            "CGST / SGST, where IGST is due"
        )
    if totals.grand_total.value is None:
        problem(f"{who} has no grand total")
    if totals.taxable_value.value is None:
        problem(f"{who} has no total taxable value")
    for rule in (totals_taxable_value, totals_tax, totals_grand_total):
        for failed in _failed(rule(invoice)):
            problem(f"{who}: {failed.message.rstrip('.')}")

    medicine = _MEDICINE if _medicine(invoice) else ()
    fields = list(dict.fromkeys((*_ALWAYS, *medicine, *required)))
    for number, line in enumerate(invoice.lines, start=1):
        for field in fields:
            if getattr(line, field).value in (None, ""):
                name = LINE_FIELD_NAMES.get(field, field)
                problem(f"{who}, line {number} has no {name}", number)

    printed = _tax_printed(invoice)
    at_rates = _tax_at_rates(invoice.lines, intra) if intra is not None else None
    if (
        printed is not None
        and at_rates is not None
        and min(abs(printed - tax) for tax in at_rates) > TOLERANCE
    ):
        problem(
            f"{who}: the tax at each line's GST rate ({money(at_rates[0])}) is not the "
            f"tax it prints ({money(printed)})"
        )
    return problems


def preflight(bills: Sequence[Bill], *, required: Sequence[str] = ()) -> tuple[BillProblem, ...]:
    """Everything to correct on `bills` before an import file books them, bill by bill.
    `required`: line fields the import layout cannot do without."""
    return tuple(problem for bill in bills for problem in _bill(bill, required))
