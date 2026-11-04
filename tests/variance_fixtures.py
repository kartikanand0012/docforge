"""Invoice and purchase-order records made from a few printed values, for the variance tests.

Each value is given as the page would print it and goes through the real normaliser, so a
record here is what a reading of such a page would be.
"""

from decimal import Decimal
from typing import Any

from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.purchase_order import (
    PurchaseOrderExtraction,
    RawPurchaseOrder,
    normalize_purchase_order,
)
from docforge.extraction.schema import InvoiceExtraction, RawInvoice
from docforge.parsing.base import Page, ParsedDocument

NO_BLOCKS = ParsedDocument(
    parser="fake", parser_version="0", pages=(Page(number=1, width=1, height=1),), blocks=()
)
SELLER_GSTIN = "24AABFN7754K1Z0"
BUYER_GSTIN = "24AAHFJ1196Q1ZU"
_BILL_LINE = (
    "product_name", "pack", "hsn", "batch_no", "mfg", "expiry", "qty", "free_qty", "mrp", "ptr",
    "discount_pct", "taxable_value", "gst_rate", "amount",
)  # fmt: skip
_ORDER_LINE = ("product_name", "pack", "hsn", "qty", "scheme", "rate", "amount")
_TOTALS = ("taxable_value", "cgst", "sgst", "igst", "round_off", "grand_total")


def _field(text: object) -> dict[str, Any]:
    return {"text": None if text is None else str(text), "block_ids": []}


def _party(name: str, gstin: str) -> dict[str, Any]:
    return {
        "name": _field(name),
        "address": _field(None),
        "gstin": _field(gstin),
        "drug_licence_nos": [],
    }


def raw_bill(
    *lines: dict[str, object],
    invoice_no: str = "NVM/26-27/1",
    invoice_date: str = "12-Oct-2026",
    po_no: str = "PO-1",
    seller: tuple[str, str] = ("Navjivan Medical Agencies", SELLER_GSTIN),
    grand_total: object = None,
) -> dict[str, Any]:
    return {
        "invoice_no": _field(invoice_no),
        "invoice_date": _field(invoice_date),
        "po_no": _field(po_no),
        "po_date": _field(None),
        "place_of_supply": _field("Gujarat"),
        "seller": _party(*seller),
        "buyer": _party("Jalaram Pharmacy", BUYER_GSTIN),
        "lines": [{name: _field(line.get(name)) for name in _BILL_LINE} for line in lines],
        "totals": {
            name: _field(grand_total if name == "grand_total" else None) for name in _TOTALS
        },
    }


def raw_order(
    *lines: dict[str, object],
    po_no: str = "PO-1",
    supplier: tuple[str, str] = ("Navjivan Medical Agencies", SELLER_GSTIN),
) -> dict[str, Any]:
    return {
        "po_no": _field(po_no),
        "po_date": _field("05-Oct-2026"),
        "buyer": _party("Jalaram Pharmacy", BUYER_GSTIN),
        "supplier_name": _field(supplier[0]),
        "supplier_gstin": _field(supplier[1]),
        "lines": [{name: _field(line.get(name)) for name in _ORDER_LINE} for line in lines],
        "total": _field(None),
    }


def bill(*lines: dict[str, object], **header: Any) -> InvoiceExtraction:
    return normalize_invoice(RawInvoice.model_validate(raw_bill(*lines, **header)), NO_BLOCKS)


def order(*lines: dict[str, object], **header: Any) -> PurchaseOrderExtraction:
    raw = RawPurchaseOrder.model_validate(raw_order(*lines, **header))
    return normalize_purchase_order(raw, NO_BLOCKS)


def billed(
    product: str = "Amoxicillin Capsules IP 250mg",
    *,
    qty: object = 200,
    ptr: object = "100.00",
    free_qty: object = None,
    discount_pct: object = None,
    taxable_value: object = "auto",
    pack: object = "10x10",
) -> dict[str, object]:
    """A bill line; its taxable value is qty x ptr less the discount unless given."""
    if taxable_value == "auto":
        value = Decimal(str(qty)) * Decimal(str(ptr))
        if discount_pct is not None:
            value = value * (100 - Decimal(str(discount_pct))) / 100
        taxable_value = f"{value.quantize(Decimal('0.01'))}"
    return {
        "product_name": product, "pack": pack, "qty": qty, "free_qty": free_qty, "ptr": ptr,
        "discount_pct": discount_pct, "taxable_value": taxable_value, "gst_rate": "12",
    }  # fmt: skip


def ordered(
    product: str = "Amoxicillin Capsules IP 250mg",
    *,
    qty: object = 200,
    rate: object = "100.00",
    scheme: object = None,
    pack: object = "10x10",
) -> dict[str, object]:
    return {"product_name": product, "pack": pack, "qty": qty, "rate": rate, "scheme": scheme}
