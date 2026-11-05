"""The PO variance of one invoice (V2 P7): per paired line, what the order agreed, what the bill
charged, and the money difference, each with its arithmetic in words.

Lines are paired exactly as the order match pairs them (`match.pair_lines`), so the variance
and the review's discrepancies always speak of the same lines. Four fields are priced:

- **Rate**: (billed rate - PO rate) x billed quantity, less the bill's own discount.
- **Qty**: (billed quantity - ordered quantity) x PO rate.
- **Free Qty**: (free quantity the PO's scheme gives on the billed quantity - free quantity
  billed) x the bill's PTR. Missing scheme goods are their own row, never merged into Qty.
- **Taxable Amount**: the bill's taxable value against the PO's terms for what was billed
  (billed quantity x PO rate, less the bill's discount), less the rate difference already
  priced, so no rupee is counted twice.

A positive money impact is the bill costing more than the PO agreed (or free goods short);
a negative one, less. Bill lines with no PO line ("Not in PO") and PO lines not billed ("Not
billed") are listed apart and never enter the net. A value that was not read is "not
compared" and carries no money. A difference inside the workspace's tolerance is shown as
matched, with a note. All money is `Decimal`, rounded to the paisa half up.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from docforge.extraction.purchase_order import OrderLineExtraction, PurchaseOrderExtraction
from docforge.extraction.schema import InvoiceExtraction, LineExtraction
from docforge.trust.match import free_quantity, pair_lines, product_key

Field = Literal["rate", "qty", "free_qty", "taxable"]
Status = Literal["variance", "within_tolerance", "not_compared"]
LABELS: dict[str, str] = {
    "rate": "Rate", "qty": "Qty", "free_qty": "Free Qty", "taxable": "Taxable Amount",
}  # fmt: skip
_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)
_ZERO = Decimal("0.00")
_MINUS = "\u2212"  # a real minus sign, as printed
_TIMES = "\u00d7"  # a multiplication sign


@dataclass(frozen=True)
class Tolerances:
    """How far a value may differ and still be matched. Rate and taxable amount in rupees,
    quantities in units. Exact by default, but a taxable amount within one rupee (rounding,
    as the 2B comparison allows)."""

    rate: Decimal = _ZERO
    qty: int = 0
    free_qty: int = 0
    amount: Decimal = Decimal("1.00")


def money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def amount(value: Decimal) -> str:
    """Rupees with India's grouping (1,00,000.00); a minus sign when below zero."""
    rounded = money(value)
    whole, paise = f"{abs(rounded):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    text = ",".join([*groups, tail]) + "." + paise
    return f"{_MINUS}{text}" if rounded < 0 else text


def inr(value: Decimal) -> str:
    text = amount(value)
    return f"{_MINUS}₹{text[1:]}" if text.startswith(_MINUS) else f"₹{text}"


def _number(value: Decimal) -> str:
    """A rate as printed: two decimals, or more when it has them."""
    return amount(value) if value == money(value) else format(value.normalize(), "f")


@dataclass(frozen=True)
class Difference:
    field: Field
    po_value: Decimal | int | None
    billed_value: Decimal | int | None
    difference: Decimal | int | None  # billed less PO
    money_impact: Decimal | None  # None when matched within tolerance or not compared
    working: str | None  # the arithmetic, in words
    status: Status
    note: str | None = None

    @property
    def label(self) -> str:
        return LABELS[self.field]


@dataclass(frozen=True)
class LineVariance:
    invoice_line: int  # 0-based
    order_line: int
    product: str | None
    differences: tuple[Difference, ...]  # every field that differs, matched or not
    note: str | None = None  # a different pack, said once

    @property
    def clean(self) -> bool:
        return all(d.status == "within_tolerance" for d in self.differences)


@dataclass(frozen=True)
class Unpaired:
    """A bill line with no PO line, or a PO line not billed."""

    line: int  # 0-based, on its own document
    product: str | None
    qty: int | None
    rate: Decimal | None
    value: Decimal | None
    working: str | None
    note: str | None = None


@dataclass(frozen=True)
class Variance:
    invoice_no: str | None
    po_no: str | None
    supplier_name: str | None
    supplier_gstin: str | None
    invoice_total: Decimal | None  # the bill's grand total as printed
    po_total: Decimal  # PO quantity x PO rate, over the paired lines
    lines: tuple[LineVariance, ...]  # every paired line, in bill order
    not_in_po: tuple[Unpaired, ...]
    not_billed: tuple[Unpaired, ...]
    tolerances: Tolerances = field(default_factory=Tolerances)

    @property
    def differing(self) -> tuple[LineVariance, ...]:
        return tuple(line for line in self.lines if not line.clean)

    @property
    def matched_lines(self) -> int:
        return len(self.lines) - len(self.differing)

    def _all(self, status: Status) -> list[Difference]:
        return [d for line in self.lines for d in line.differences if d.status == status]

    @property
    def variances(self) -> int:
        return len(self._all("variance"))

    @property
    def not_compared(self) -> int:
        return len(self._all("not_compared"))

    @property
    def net_impact(self) -> Decimal:
        return money(sum((d.money_impact or _ZERO for d in self._all("variance")), _ZERO))

    @property
    def above_po(self) -> Decimal:
        """The money the bill charges beyond the PO: the positive impacts only."""
        impacts = (d.money_impact or _ZERO for d in self._all("variance"))
        return money(sum((i for i in impacts if i > 0), _ZERO))

    @property
    def net_percent(self) -> Decimal | None:
        return money(self.net_impact / self.po_total * _HUNDRED) if self.po_total else None

    @property
    def not_in_po_value(self) -> Decimal:
        return money(sum((u.value for u in self.not_in_po if u.value is not None), _ZERO))

    @property
    def not_billed_value(self) -> Decimal:
        return money(sum((u.value for u in self.not_billed if u.value is not None), _ZERO))


def _not_read(name: str, bill: object, po: object) -> str:
    if bill is None and po is None:
        return f"The {name} was read on neither the bill nor the PO, so it could not be compared."
    side = "bill's" if bill is None else "PO's"
    return f"The {side} {name} was not read, so it could not be compared."


def _inside(name: str, by: str, tolerance: str) -> str:
    return f"{name} differs by {by}, inside the workspace's tolerance of {tolerance}."


def _status(over: bool) -> Status:
    return "variance" if over else "within_tolerance"


def _rate(
    b: LineExtraction, o: OrderLineExtraction, tol: Tolerances, factor: Decimal, less: str
) -> tuple[list[Difference], Decimal]:
    """The rate's row, and the rupees it accounts for (even inside the tolerance), which the
    taxable amount must not count again."""
    bq, br, pr = b.qty.value, b.ptr.value, o.rate.value
    if br is None or pr is None:
        return [Difference("rate", pr, br, None, None, None, "not_compared",
                           _not_read("rate", br, pr))], _ZERO  # fmt: skip
    if br == pr:
        return [], _ZERO
    gap = br - pr
    if bq is None:
        return [Difference("rate", pr, br, gap, None, None, "not_compared",
                           "The bill's quantity was not read, so the rate difference could "
                           "not be priced.")], _ZERO  # fmt: skip
    effect = money(gap * bq * factor)
    over = abs(gap) > tol.rate
    return [Difference(
        "rate", pr, br, gap, effect if over else None,
        f"({_number(br)} {_MINUS} {_number(pr)}) {_TIMES} {bq} billed{less} = {inr(effect)}",
        _status(over),
        None if over else _inside("Rate", _number(abs(gap)), _number(tol.rate)),
    )], effect  # fmt: skip


def _qty(b: LineExtraction, o: OrderLineExtraction, tol: Tolerances) -> list[Difference]:
    bq, pq, pr = b.qty.value, o.qty.value, o.rate.value
    if bq is None or pq is None:
        return [Difference("qty", pq, bq, None, None, None, "not_compared",
                           _not_read("quantity", bq, pq))]  # fmt: skip
    if bq == pq:
        return []
    gap = bq - pq
    if pr is None:
        return [Difference("qty", pq, bq, gap, None, None, "not_compared",
                           "The PO's rate was not read, so the quantity difference could not "
                           "be priced.")]  # fmt: skip
    impact = money(gap * pr)
    over = abs(gap) > tol.qty
    return [Difference(
        "qty", pq, bq, gap, impact if over else None,
        f"({bq} billed {_MINUS} {pq} ordered) {_TIMES} PO rate {_number(pr)} = {inr(impact)}",
        _status(over),
        None if over else _inside("Qty", str(abs(gap)), str(tol.qty)),
    )]  # fmt: skip


def _free(b: LineExtraction, o: OrderLineExtraction, tol: Tolerances) -> list[Difference]:
    """Free goods as the match judges them: the PO's scheme applied to the billed quantity."""
    bq = b.qty.value
    if bq is None:
        return []  # the quantity is already "not compared"
    if o.scheme.raw is not None and o.scheme.value is None:
        return [Difference("free_qty", None, b.free_qty.value, None, None, None, "not_compared",
                           f"The PO's scheme ({o.scheme.raw}) could not be read, so free "
                           "quantity could not be compared.")]  # fmt: skip
    due = free_quantity(o.scheme.value, bq)
    got = b.free_qty.value or 0
    if got == due:
        return []
    scheme = (
        f"Scheme {o.scheme.value} on {bq} billed gives {due} free."
        if o.scheme.value
        else f"The PO has no scheme; the bill gives {got} free."
    )
    price, priced = b.ptr.value, "PTR"
    if price is None:
        price, priced = o.rate.value, "PO rate"
        scheme += " The bill's PTR was not read, so the PO rate is used."
    if price is None:
        return [Difference("free_qty", due, got, got - due, None, None, "not_compared",
                           f"{scheme} No rate was read to value it.")]  # fmt: skip
    impact = money((due - got) * price)
    over = abs(got - due) > tol.free_qty
    inside = _inside("Free Qty", str(abs(got - due)), str(tol.free_qty))
    return [Difference(
        "free_qty", due, got, got - due, impact if over else None,
        f"({due} due {_MINUS} {got} billed) {_TIMES} {priced} {_number(price)} = {inr(impact)}",
        _status(over), scheme if over else f"{scheme} {inside}",
    )]  # fmt: skip


def _taxable(
    b: LineExtraction, o: OrderLineExtraction, tol: Tolerances, factor: Decimal,
    discount: Decimal, rate_effect: Decimal,
) -> list[Difference]:  # fmt: skip
    """The bill's taxable value against the PO's terms for the billed quantity. A bill that
    prints no line taxable value is compared on rate and quantity alone."""
    bt, bq, br, pr = b.taxable_value.value, b.qty.value, b.ptr.value, o.rate.value
    if bt is None or bq is None or br is None or pr is None:
        return []
    terms = money(bq * pr * factor)
    gap = bt - terms
    residual = money(gap - rate_effect)
    if not residual:
        return []
    less = f" less {format(discount.normalize(), 'f')}%" if discount else ""
    terms_text = f"{bq} {_TIMES} {_number(pr)}{less} = {amount(terms)}"
    working = f"{amount(bt)} billed {_MINUS} PO terms {terms_text} → "
    if rate_effect:
        working += (
            f"{amount(gap)}, less the rate difference {amount(rate_effect)} = {inr(residual)}"
        )
    else:
        working += inr(residual)
    over = abs(residual) > tol.amount
    return [Difference(
        "taxable", terms, bt, money(gap), residual if over else None, working, _status(over),
        None if over else _inside("Taxable Amount", amount(abs(residual)), amount(tol.amount)),
    )]  # fmt: skip


def _line(
    index: int, other: int, b: LineExtraction, o: OrderLineExtraction, tol: Tolerances
) -> LineVariance:
    discount = b.discount_pct.value or Decimal(0)
    factor = (_HUNDRED - discount) / _HUNDRED
    less = f", less {format(discount.normalize(), 'f')}% discount" if discount else ""
    rate, rate_effect = _rate(b, o, tol, factor, less)
    found = [*rate, *_qty(b, o, tol), *_free(b, o, tol),
             *_taxable(b, o, tol, factor, discount, rate_effect)]  # fmt: skip
    note = None
    if b.pack.value and o.pack.value and b.pack.value != o.pack.value:
        note = f"Pack differs: bill {b.pack.value}, PO {o.pack.value}. Check before acting on it."
    return LineVariance(index, other, b.product_name.value or o.product_name.value,
                        tuple(found), note)  # fmt: skip


def _not_in_po(invoice: InvoiceExtraction, index: int, paired_at: int | None) -> Unpaired:
    b = invoice.lines[index]
    value, working = b.taxable_value.value, "taxable amount as billed"
    if value is None and b.qty.value is not None and b.ptr.value is not None:
        value = money(b.qty.value * b.ptr.value)
        working = f"{b.qty.value} {_TIMES} {_number(b.ptr.value)} = {inr(value)}"
    return Unpaired(
        index, b.product_name.value, b.qty.value, b.ptr.value, value,
        working if value is not None else None,
        None if paired_at is None
        else f"This product is on the PO, paired with bill line {paired_at + 1} (another batch?).",
    )  # fmt: skip


def _not_billed(order: PurchaseOrderExtraction, index: int) -> Unpaired:
    o = order.lines[index]
    value = working = None
    if o.qty.value is not None and o.rate.value is not None:
        value = money(o.qty.value * o.rate.value)
        working = f"{o.qty.value} {_TIMES} {_number(o.rate.value)} = {inr(value)}"
    return Unpaired(
        index, o.product_name.value, o.qty.value, o.rate.value, value, working,
        None if o.product_name.value
        else "The PO line's product could not be read, so it could not be compared.",
    )  # fmt: skip


def compare(
    invoice: InvoiceExtraction, order: PurchaseOrderExtraction, tolerances: Tolerances | None = None
) -> Variance:
    """The variance of a bill against the PO it was billed against."""
    tol = tolerances or Tolerances()
    pairing = pair_lines(invoice, order)
    lines: list[LineVariance] = []
    unpaired: list[int] = []
    first_paired: dict[str, int] = {}  # product -> the first bill line paired with it
    for index, other, _ in pairing.billed:
        if other is None:
            unpaired.append(index)
            continue
        lines.append(_line(index, other, invoice.lines[index], order.lines[other], tol))
        first_paired.setdefault(product_key(invoice.lines[index].product_name.value or ""), index)
    not_in_po = tuple(
        _not_in_po(invoice, index, first_paired.get(product_key(name)) if name else None)
        for index in unpaired
        for name in (invoice.lines[index].product_name.value,)
    )
    not_billed = tuple(
        _not_billed(order, index) for index in (*pairing.unread_orders, *pairing.not_supplied)
    )
    po_total = _ZERO
    for line in lines:
        o = order.lines[line.order_line]
        if o.qty.value is not None and o.rate.value is not None:
            po_total += o.qty.value * o.rate.value
    return Variance(
        invoice_no=invoice.invoice_no.value,
        po_no=order.po_no.value or invoice.po_no.value,
        supplier_name=invoice.seller.name.value or order.supplier_name.value,
        supplier_gstin=invoice.seller.gstin.value or order.supplier_gstin.value,
        invoice_total=invoice.totals.grand_total.value,
        po_total=money(po_total),
        lines=tuple(lines),
        not_in_po=not_in_po,
        not_billed=not_billed,
        tolerances=tol,
    )
