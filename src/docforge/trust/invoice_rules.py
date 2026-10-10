"""Rules for the invoice document type.

Each rule reports `not_evaluated` when a value it needs is missing; it never guesses one.
"""

import re
from collections.abc import Callable, Iterable, Iterator
from decimal import ROUND_HALF_UP, Decimal

from docforge.extraction.schema import InvoiceExtraction, LineExtraction
from docforge.gstin import is_valid_gstin
from docforge.trust.rules import Outcome, RuleResult, Severity

_CENT = Decimal("0.01")
_TOLERANCE = Decimal("0.01")
_HSN = re.compile(r"\d{4}(\d{2}){0,2}", re.ASCII)  # 4, 6 or 8 digits
_MIN_SHELF_LIFE_MONTHS = 3


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _months(month: str) -> int:
    """`2028-03` as a count of months, for comparing."""
    return int(month[:4]) * 12 + int(month[5:7])


def _result(
    rule_id: str,
    severity: Severity,
    paths: tuple[str, ...],
    ok: bool | None,
    message: str,
    version: int = 1,
) -> RuleResult:
    outcome: Outcome = "not_evaluated" if ok is None else ("passed" if ok else "failed")
    if ok is None:
        message = "A value this check needs is missing."
    elif ok:
        message = "OK"
    return RuleResult(
        rule_id=rule_id,
        version=version,
        severity=severity,
        outcome=outcome,
        message=message,
        paths=paths,
    )


def _lines(invoice: InvoiceExtraction) -> Iterator[tuple[str, LineExtraction]]:
    for index, line in enumerate(invoice.lines):
        yield f"lines[{index}]", line


def _intra_state(invoice: InvoiceExtraction) -> bool | None:
    seller, buyer = invoice.seller.gstin.value, invoice.buyer.gstin.value
    if not seller or not buyer:
        return None
    return seller[:2] == buyer[:2]


def gstin_checksum(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    for party in ("seller", "buyer"):
        gstin = getattr(invoice, party).gstin.value
        yield _result(
            "gstin.checksum",
            "error",
            (f"{party}.gstin",),
            None if gstin is None else is_valid_gstin(gstin),
            f"{gstin} is not a valid GSTIN: its format or check character is wrong.",
        )


def place_of_supply_matches_buyer(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    gstin, code = invoice.buyer.gstin.value, invoice.place_of_supply_code.value
    yield _result(
        "gstin.place_of_supply",
        "warning",
        ("place_of_supply_code", "buyer.gstin"),
        None if gstin is None or code is None else gstin[:2] == code,
        f"Place of supply code {code} differs from the buyer's GSTIN state code.",
    )


def hsn_format(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    for path, line in _lines(invoice):
        hsn = line.hsn.value
        yield _result(
            "hsn.format",
            "warning",
            (f"{path}.hsn",),
            None if hsn is None else _HSN.fullmatch(hsn) is not None,
            f"HSN {hsn} is not 4, 6 or 8 digits.",
        )


def line_dates(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    issued = invoice.invoice_date.value
    for path, line in _lines(invoice):
        mfg, expiry = line.mfg.value, line.expiry.value
        ok = None
        if mfg is not None and expiry is not None and issued is not None:
            ok = _months(mfg) < _months(expiry) and _months(mfg) <= issued.year * 12 + issued.month
        yield _result(
            "line.dates",
            "error",
            (f"{path}.mfg", f"{path}.expiry"),
            ok,
            f"Manufacture {mfg} must be before expiry {expiry} and not after the invoice date.",
        )


def line_not_expired(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    issued = invoice.invoice_date.value
    for path, line in _lines(invoice):
        expiry = line.expiry.value
        remaining = None
        if expiry is not None and issued is not None:
            remaining = _months(expiry) - (issued.year * 12 + issued.month)
        yield _result(
            "line.not_expired",
            "error",
            (f"{path}.expiry",),
            None if remaining is None else remaining >= 0,
            f"Stock expired {expiry}, before the invoice date {issued}.",
        )
        if remaining is not None and remaining >= 0:
            yield _result(
                "line.shelf_life",
                "warning",
                (f"{path}.expiry",),
                remaining >= _MIN_SHELF_LIFE_MONTHS,
                f"Stock expires {expiry}, less than {_MIN_SHELF_LIFE_MONTHS} months after "
                "the invoice date.",
            )


def line_ptr_not_above_mrp(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    for path, line in _lines(invoice):
        ptr, mrp = line.ptr.value, line.mrp.value
        yield _result(
            "line.ptr_not_above_mrp",
            "error",
            (f"{path}.ptr",),
            None if ptr is None or mrp is None else ptr <= mrp,
            f"Price to retailer {ptr} is above the maximum retail price {mrp}.",
        )


def line_taxable_value(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    for path, line in _lines(invoice):
        qty, ptr, discount = line.qty.value, line.ptr.value, line.discount_pct.value
        taxable = line.taxable_value.value
        ok, expected = None, None
        if qty is not None and ptr is not None and taxable is not None:
            # A blank discount is read as none, which is how invoices print it.
            expected = _money(qty * ptr * (Decimal(100) - (discount or Decimal(0))) / Decimal(100))
            ok = abs(expected - taxable) <= _TOLERANCE
        yield _result(
            "line.taxable_value",
            "error",
            (f"{path}.taxable_value",),
            ok,
            f"Taxable value {taxable} is not quantity x rate less discount ({expected}).",
        )


def line_amount(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    intra = _intra_state(invoice)
    for path, line in _lines(invoice):
        taxable, rate, amount = line.taxable_value.value, line.gst_rate.value, line.amount.value
        ok, expected = None, None
        if taxable is not None and rate is not None and amount is not None:
            # Within a state the tax is two halves rounded separately; across states it is one.
            split = taxable + 2 * _money(taxable * rate / Decimal(200))
            whole = taxable + _money(taxable * rate / Decimal(100))
            allowed = {True: (split,), False: (whole,), None: (split, whole)}[intra]
            expected = allowed[0]
            ok = min(abs(amount - value) for value in allowed) <= _TOLERANCE
        yield _result(
            "line.amount",
            "error",
            (f"{path}.amount",),
            ok,
            f"Line amount {amount} is not taxable value plus tax ({expected}).",
        )


def totals_taxable_value(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    values = [line.taxable_value.value for line in invoice.lines]
    total = invoice.totals.taxable_value.value
    ok, expected = None, None
    if total is not None and values and all(value is not None for value in values):
        expected = sum((value for value in values if value is not None), Decimal(0))
        ok = abs(expected - total) <= _TOLERANCE
    yield _result(
        "totals.taxable_value",
        "error",
        ("totals.taxable_value",),
        ok,
        f"Total taxable value {total} is not the sum of the lines ({expected}).",
    )


def totals_tax(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    totals = invoice.totals
    printed = [value for value in (totals.cgst.value, totals.sgst.value, totals.igst.value)]
    ok, expected, tax = None, None, None
    differences = [
        amount - taxable
        for line in invoice.lines
        if (amount := line.amount.value) is not None
        and (taxable := line.taxable_value.value) is not None
    ]
    if any(value is not None for value in printed) and len(differences) == len(invoice.lines) > 0:
        expected = sum(differences, Decimal(0))
        tax = sum((value for value in printed if value is not None), Decimal(0))
        ok = abs(expected - tax) <= _TOLERANCE
    yield _result(
        "totals.tax",
        "error",
        ("totals.cgst", "totals.sgst", "totals.igst"),
        ok,
        f"Total tax {tax} is not the sum of the tax on the lines ({expected}).",
    )


def totals_grand_total(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    totals = invoice.totals
    taxable, round_off, grand = (
        totals.taxable_value.value,
        totals.round_off.value,
        totals.grand_total.value,
    )
    ok, expected = None, None
    if taxable is not None and grand is not None:
        tax = sum(
            (v for v in (totals.cgst.value, totals.sgst.value, totals.igst.value) if v is not None),
            Decimal(0),
        )
        expected = taxable + tax + (round_off or Decimal(0))
        ok = abs(expected - grand) <= _TOLERANCE and abs(round_off or Decimal(0)) <= Decimal("0.50")
    yield _result(
        "totals.grand_total",
        "error",
        ("totals.grand_total",),
        ok,
        f"Grand total {grand} is not taxable value plus tax plus round-off ({expected}), "
        "or the round-off is more than half a rupee.",
    )


def tax_matches_supply_type(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    intra = _intra_state(invoice)
    totals = invoice.totals
    if intra is None:
        yield _result("tax.matches_supply_type", "error", ("seller.gstin", "buyer.gstin"), None, "")
        return
    unexpected = ("igst",) if intra else ("cgst", "sgst")
    wrong = tuple(f"totals.{name}" for name in unexpected if getattr(totals, name).value)
    kind = "within one state" if intra else "between states"
    yield _result(
        "tax.matches_supply_type",
        "error",
        wrong or tuple(f"totals.{name}" for name in unexpected),
        not wrong,
        f"A supply {kind} must not carry {' or '.join(name.upper() for name in unexpected)}.",
    )


def required_present(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
    # Each path with its value and the text printed for it.
    required: dict[str, tuple[object, str | None]] = {
        path: (field.value, field.raw)
        for path, field in (
            ("invoice_no", invoice.invoice_no),
            ("invoice_date", invoice.invoice_date),
            ("seller.gstin", invoice.seller.gstin),
            ("buyer.gstin", invoice.buyer.gstin),
            ("totals.grand_total", invoice.totals.grand_total),
        )
    }
    required["lines"] = (invoice.lines or None, None)
    for path, line in _lines(invoice):
        for name in ("product_name", "batch_no", "expiry", "qty"):
            field = getattr(line, name)
            required[f"{path}.{name}"] = (field.value, field.raw)
    for path, (value, raw) in required.items():
        # A value printed but not readable (a quantity of 7.5) is not the same as no value.
        message = (
            f"{path} is missing."
            if raw is None
            else f"{path} is printed as {raw!r} but could not be read."
        )
        yield _result("required.present", "error", (path,), value is not None, message)


INVOICE_RULES: tuple[Callable[[InvoiceExtraction], Iterable[RuleResult]], ...] = (
    required_present,
    gstin_checksum,
    place_of_supply_matches_buyer,
    hsn_format,
    line_dates,
    line_not_expired,
    line_ptr_not_above_mrp,
    line_taxable_value,
    line_amount,
    totals_taxable_value,
    totals_tax,
    totals_grand_total,
    tax_matches_supply_type,
)
