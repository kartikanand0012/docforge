"""Ground-truth schema for the synthetic documents."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# Money has at most two decimal places and is written to JSON as a two-place string.
Money = Annotated[
    Decimal,
    Field(decimal_places=2),
    PlainSerializer(lambda value: f"{value:.2f}", return_type=str),
]
# Calendar month, e.g. "2028-03". Printed on documents as MM/YY.
Month = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Layout = Literal["A", "B"]
SupplyType = Literal["intra_state", "inter_state"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Party(_Model):
    name: str
    address: str
    state: str
    state_code: str
    gstin: str
    drug_licence_nos: tuple[str, ...]


class InvoiceLine(_Model):
    sl_no: int
    product_name: str
    pack: str
    hsn: str
    batch_no: str
    mfg: Month
    expiry: Month
    qty: int
    free_qty: int
    mrp: Money
    ptr: Money
    discount_pct: Decimal
    taxable_value: Money
    gst_rate: Decimal
    cgst: Money
    sgst: Money
    igst: Money
    amount: Money


class InvoiceTotals(_Model):
    taxable_value: Money
    cgst: Money
    sgst: Money
    igst: Money
    round_off: Money
    grand_total: Money


class Invoice(_Model):
    invoice_no: str
    invoice_date: date
    po_no: str
    po_date: date
    seller: Party
    buyer: Party
    place_of_supply: str
    place_of_supply_code: str
    supply_type: SupplyType
    lines: tuple[InvoiceLine, ...]
    totals: InvoiceTotals


class PurchaseOrderLine(_Model):
    sl_no: int
    product_name: str
    pack: str
    qty: int
    scheme: str | None
    rate: Money


class PurchaseOrder(_Model):
    po_no: str
    po_date: date
    buyer: Party
    supplier_name: str
    supplier_gstin: str
    lines: tuple[PurchaseOrderLine, ...]


class DocumentPair(_Model):
    pair_id: str
    layout: Layout
    invoice: Invoice
    purchase_order: PurchaseOrder


class FieldBox(_Model):
    """Where one ground-truth value is printed. PDF points, origin at the bottom-left."""

    path: str
    text: str
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class DocumentBoxes(_Model):
    """Where the ground truth sits in one PDF.

    Every value of the document's label is either in `boxes` or listed in `unprinted`.
    Unprinted values are implied by the document (for example the supply type) but appear
    nowhere on the page, so an extractor must not be scored on reading them.
    """

    file: str
    page_width: float
    page_height: float
    boxes: tuple[FieldBox, ...]
    unprinted: tuple[str, ...]


def leaf_paths(model: BaseModel) -> set[str]:
    """Path of every scalar value in `model`, e.g. `lines[0].batch_no`, `seller.gstin`."""

    def walk(value: object, prefix: str) -> set[str]:
        if isinstance(value, dict):
            return {
                path
                for key, item in value.items()
                for path in walk(item, f"{prefix}.{key}" if prefix else key)
            }
        if isinstance(value, list):
            return {
                path
                for index, item in enumerate(value)
                for path in walk(item, f"{prefix}[{index}]")
            }
        return {prefix}

    return walk(model.model_dump(mode="json"), "")


class PairLabel(_Model):
    schema_version: Literal["1"] = "1"
    pair_id: str
    seed: int
    layout: Layout
    invoice: Invoice
    purchase_order: PurchaseOrder
    documents: dict[str, DocumentBoxes]
