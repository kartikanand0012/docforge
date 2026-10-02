"""Compare an invoice with the purchase order it claims to fulfil."""

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
                severity="warning",
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
    if billed.qty.value is not None and billed.free_qty.value is not None:
        # A scheme that was printed but could not be read is unknown, not absent.
        if ordered.scheme.raw is not None and ordered.scheme.value is None:
            return found
        expected = _free_quantity(ordered.scheme.value, billed.qty.value)
        if billed.free_qty.value != expected:
            found.append(
                Discrepancy(
                    code="line.free_qty",
                    severity="error",
                    message=f"Free quantity {billed.free_qty.value} does not follow the ordered "
                    f"scheme ({ordered.scheme.value or 'none'}), which gives {expected}.",
                    invoice_path=f"{invoice_path}.free_qty",
                    order_path=f"{order_path}.scheme",
                    invoice_value=str(billed.free_qty.value),
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
    ordered = {
        line.product_name.value: (f"lines[{index}]", line)
        for index, line in enumerate(order.lines)
        if line.product_name.value
    }
    for index, billed in enumerate(invoice.lines):
        path = f"lines[{index}]"
        pair = ordered.pop(billed.product_name.value or "", None)
        if pair is None:
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
        found += _line(path, billed, *pair)
    for order_path, line in ordered.values():
        found.append(
            Discrepancy(
                code="line.not_supplied",
                severity="warning",
                message=f"{line.product_name.value} was ordered but is not on the invoice.",
                invoice_path=None,
                order_path=f"{order_path}.product_name",
                invoice_value=None,
                order_value=line.product_name.value,
            )
        )
    return tuple(found)
