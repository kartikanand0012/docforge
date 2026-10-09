"""Compare an invoice with the purchase order it claims to fulfil."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from docforge.extraction.purchase_order import OrderLineExtraction, PurchaseOrderExtraction
from docforge.extraction.schema import Extracted, InvoiceExtraction, LineExtraction


class Discrepancy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    severity: Literal["error", "warning"]
    message: str
    invoice_path: str | None
    order_path: str | None
    invoice_value: str | None
    order_value: str | None


def _key(product_name: str) -> str:
    """Product names are compared without regard to case or spacing."""
    return " ".join(product_name.split()).casefold()


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _differs[T](
    code: str,
    what: str,
    invoice: tuple[str, Extracted[T]],
    order: tuple[str, Extracted[T]],
) -> list[Discrepancy]:
    (invoice_path, billed), (order_path, ordered) = invoice, order
    if billed.value is None or ordered.value is None:
        # Not comparable is not the same as different; a reviewer has to look.
        return [
            Discrepancy(
                code=f"{code.split('.')[0]}.unchecked" if "." in code else f"{code}.unchecked",
                severity="error",  # not comparable means a person must look
                message=f"{what} could not be compared: a value is missing.",
                invoice_path=invoice_path,
                order_path=order_path,
                invoice_value=_text(billed.value),
                order_value=_text(ordered.value),
            )
        ]
    if billed.value == ordered.value:
        return []
    return [
        Discrepancy(
            code=code,
            severity="error",
            message=f"{what} on the invoice ({billed.value}) differs from the order "
            f"({ordered.value}).",
            invoice_path=invoice_path,
            order_path=order_path,
            invoice_value=_text(billed.value),
            order_value=_text(ordered.value),
        )
    ]


def _warning(
    code: str,
    message: str,
    invoice: tuple[str, object],
    order: tuple[str, object],
) -> Discrepancy:
    return Discrepancy(
        code=code,
        severity="warning",
        message=message,
        invoice_path=invoice[0],
        order_path=order[0],
        invoice_value=_text(invoice[1]),
        order_value=_text(order[1]),
    )


def _hsn_key(hsn: str) -> str:
    return "".join(hsn.split())


def _total(invoice: InvoiceExtraction, order: PurchaseOrderExtraction) -> list[Discrepancy]:
    """The order's total against the invoice's. A PO total may be printed with tax or
    without it, so it agrees if it equals either the invoice's grand total or its taxable
    value. Only a warning: a part shipment or a changed price is billed for less or more,
    and the lines are compared one by one anyway."""
    ordered = order.total.value
    grand, taxable = invoice.totals.grand_total.value, invoice.totals.taxable_value.value
    billed = [value for value in (grand, taxable) if value is not None]
    if ordered is None or not billed:
        return []
    if any(abs(value - ordered) <= Decimal("0.01") for value in billed):
        return []
    return [
        _warning(
            "order.total",
            f"The order total ({ordered}) is neither the invoice's grand total ({grand}) nor "
            f"its taxable value ({taxable}).",
            ("totals.grand_total", grand),
            ("total", ordered),
        )
    ]


def _free_quantity(scheme: str | None, qty: int) -> int:
    if scheme is None:
        return 0
    buy, free = (int(part) for part in scheme.split("+"))
    return (qty // buy) * free if buy else 0


def _line(
    invoice_path: str, billed: LineExtraction, order_path: str, ordered: OrderLineExtraction
) -> list[Discrepancy]:
    found = _differs(
        "line.qty",
        "Quantity",
        (f"{invoice_path}.qty", billed.qty),
        (f"{order_path}.qty", ordered.qty),
    )
    found += _differs(
        "line.rate",
        "Rate",
        (f"{invoice_path}.ptr", billed.ptr),
        (f"{order_path}.rate", ordered.rate),
    )
    if billed.pack.value and ordered.pack.value and billed.pack.value != ordered.pack.value:
        found += _differs(
            "line.pack",
            "Pack",
            (f"{invoice_path}.pack", billed.pack),
            (f"{order_path}.pack", ordered.pack),
        )
    billed_hsn, ordered_hsn = billed.hsn.value, ordered.hsn.value
    if billed_hsn and ordered_hsn and _hsn_key(billed_hsn) != _hsn_key(ordered_hsn):
        # A warning: the classification is the supplier's to state, and it does not change
        # what was ordered; a person decides whether it matters.
        found.append(
            _warning(
                "line.hsn",
                f"HSN on the invoice ({billed_hsn}) differs from the order ({ordered_hsn}).",
                (f"{invoice_path}.hsn", billed_hsn),
                (f"{order_path}.hsn", ordered_hsn),
            )
        )
    # A scheme that was printed but could not be read is unknown, not absent.
    if ordered.scheme.raw is not None and ordered.scheme.value is None:
        return found
    if billed.qty.value is not None:
        expected = _free_quantity(ordered.scheme.value, billed.qty.value)
        # A blank free quantity is zero, which is wrong if the order promised free goods.
        if (billed.free_qty.value or 0) != expected:
            found.append(
                Discrepancy(
                    code="line.free_qty",
                    severity="error",
                    message=f"Free quantity {billed.free_qty.value or 0} does not follow the "
                    f"ordered scheme ({ordered.scheme.value or 'none'}), which gives {expected}.",
                    invoice_path=f"{invoice_path}.free_qty",
                    order_path=f"{order_path}.scheme",
                    invoice_value=_text(billed.free_qty.value),
                    order_value=ordered.scheme.value,
                )
            )
    return found


def match_invoice_to_order(
    invoice: InvoiceExtraction, order: PurchaseOrderExtraction
) -> tuple[Discrepancy, ...]:
    """Every difference between what was ordered and what was billed. Empty means they agree."""
    found = _differs("po_no", "Order number", ("po_no", invoice.po_no), ("po_no", order.po_no))
    found += _differs(
        "supplier.gstin",
        "Supplier GSTIN",
        ("seller.gstin", invoice.seller.gstin),
        ("supplier_gstin", order.supplier_gstin),
    )
    found += _differs(
        "buyer.gstin",
        "Buyer GSTIN",
        ("buyer.gstin", invoice.buyer.gstin),
        ("buyer.gstin", order.buyer.gstin),
    )
    issued, placed = invoice.invoice_date.value, order.po_date.value
    if issued is not None and placed is not None and placed > issued:
        found.append(
            Discrepancy(
                code="po_date",
                severity="error",
                message=f"The order is dated {placed}, after the invoice ({issued}).",
                invoice_path="invoice_date",
                order_path="po_date",
                invoice_value=str(issued),
                order_value=str(placed),
            )
        )

    # Lines are paired by product, because an invoice need not list them in order sequence.
    # The same product may appear on several lines (one per batch), so each name keeps a list.
    ordered: dict[str, list[tuple[str, OrderLineExtraction]]] = {}
    for index, line in enumerate(order.lines):
        if line.product_name.value:
            ordered.setdefault(_key(line.product_name.value), []).append((f"lines[{index}]", line))
        else:
            found.append(
                Discrepancy(
                    code="line.unchecked",
                    severity="error",
                    message="An order line has no readable product, so it could not be compared.",
                    invoice_path=None,
                    order_path=f"lines[{index}].product_name",
                    invoice_value=None,
                    order_value=None,
                )
            )
    for index, billed in enumerate(invoice.lines):
        path = f"lines[{index}]"
        candidates = ordered.get(_key(billed.product_name.value or ""), [])
        if not candidates:
            found.append(
                Discrepancy(
                    code="line.not_ordered",
                    severity="error",
                    message=f"{billed.product_name.value or 'A line'} is billed but not ordered.",
                    invoice_path=f"{path}.product_name",
                    order_path=None,
                    invoice_value=billed.product_name.value,
                    order_value=None,
                )
            )
            continue
        # Of several order lines for this product, take the one this line agrees with most.
        differences = [_line(path, billed, *candidate) for candidate in candidates]
        best = min(range(len(candidates)), key=lambda i: len(differences[i]))
        candidates.pop(best)
        found += differences[best]
    for remaining in ordered.values():
        for order_path, line in remaining:
            found.append(
                Discrepancy(
                    code="line.not_supplied",
                    severity="error",  # may be a part shipment, but someone has to confirm it
                    message=f"{line.product_name.value} was ordered but is not on the invoice.",
                    invoice_path=None,
                    order_path=f"{order_path}.product_name",
                    invoice_value=None,
                    order_value=line.product_name.value,
                )
            )
    found += _total(invoice, order)
    return tuple(found)
