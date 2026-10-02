"""Invoice extraction schemas.

`Raw*` is what the model returns: the printed string of each field and the blocks it came
from. `*Extraction` is what the API returns: the same fields converted to typed values by
code (see `normalize.py`), with the printed string kept beside each value.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --- model output ---------------------------------------------------------------------------


class RawField(BaseModel):
    text: str | None = Field(
        description="The value exactly as printed, without its label. null if it is not printed."
    )
    block_ids: list[str] = Field(
        description="Ids of the blocks the value was read from, e.g. ['b12']. Empty if null."
    )


class RawParty(BaseModel):
    name: RawField
    address: RawField
    gstin: RawField
    drug_licence_nos: list[RawField] = Field(description="One entry per drug licence number.")


class RawLine(BaseModel):
    product_name: RawField = Field(description="Product description, without the serial number.")
    pack: RawField = Field(description="Pack size, e.g. 10x10.")
    hsn: RawField
    batch_no: RawField
    mfg: RawField = Field(description="Manufacturing month as printed, e.g. 06/26.")
    expiry: RawField = Field(description="Expiry month as printed, e.g. 06/28.")
    qty: RawField = Field(description="Billed quantity.")
    free_qty: RawField = Field(description="Free or scheme quantity.")
    mrp: RawField = Field(description="Maximum retail price.")
    ptr: RawField = Field(description="Price to retailer; may be labelled Rate.")
    discount_pct: RawField
    taxable_value: RawField
    gst_rate: RawField = Field(description="GST or tax percentage.")
    amount: RawField = Field(description="Line total including tax.")


class RawTotals(BaseModel):
    taxable_value: RawField
    cgst: RawField
    sgst: RawField
    igst: RawField
    round_off: RawField
    grand_total: RawField


class RawInvoice(BaseModel):
    invoice_no: RawField
    invoice_date: RawField
    po_no: RawField = Field(description="Purchase order number or order reference.")
    po_date: RawField
    place_of_supply: RawField
    seller: RawParty
    buyer: RawParty
    lines: list[RawLine] = Field(description="One entry per line item, in printed order.")
    totals: RawTotals


# --- API output -----------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Extracted[T](_Model):
    """One field. `value` is null when the field is absent or its text could not be read."""

    value: T | None
    raw: str | None
    block_ids: tuple[str, ...]


class Issue(_Model):
    path: str
    code: Literal["unparseable", "unknown_block"]
    message: str


class PartyExtraction(_Model):
    name: Extracted[str]
    address: Extracted[str]
    gstin: Extracted[str]
    drug_licence_nos: tuple[Extracted[str], ...]


class LineExtraction(_Model):
    product_name: Extracted[str]
    pack: Extracted[str]
    hsn: Extracted[str]
    batch_no: Extracted[str]
    mfg: Extracted[str]  # YYYY-MM
    expiry: Extracted[str]  # YYYY-MM
    qty: Extracted[int]
    free_qty: Extracted[int]
    mrp: Extracted[Decimal]
    ptr: Extracted[Decimal]
    discount_pct: Extracted[Decimal]
    taxable_value: Extracted[Decimal]
    gst_rate: Extracted[Decimal]
    amount: Extracted[Decimal]


class TotalsExtraction(_Model):
    taxable_value: Extracted[Decimal]
    cgst: Extracted[Decimal]
    sgst: Extracted[Decimal]
    igst: Extracted[Decimal]
    round_off: Extracted[Decimal]
    grand_total: Extracted[Decimal]


class InvoiceExtraction(_Model):
    schema_version: Literal["invoice-1"] = "invoice-1"
    invoice_no: Extracted[str]
    invoice_date: Extracted[date]
    po_no: Extracted[str]
    po_date: Extracted[date]
    place_of_supply: Extracted[str]
    place_of_supply_code: Extracted[str]
    seller: PartyExtraction
    buyer: PartyExtraction
    lines: tuple[LineExtraction, ...]
    totals: TotalsExtraction
    issues: tuple[Issue, ...]
