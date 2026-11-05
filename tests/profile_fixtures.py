"""Made-up bills and mappings for the import-profile engine (V2 P3). Every name, number and
GSTIN here is synthetic; none is from a real bill or a real Marg import.

SYN/0001: a Gujarat seller, place of supply 24 (within its state: CGST + SGST), two lines at
12% and 5%, 2 free on the first. SYN/0002: a Maharashtra seller supplying Gujarat (IGST),
4 free. `tests/fixtures/profiles/SAMPLE-synthetic-purchase-import.csv` is these bills in the
SAMPLE layout, typed by hand.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docforge.extraction.schema import InvoiceExtraction
from docforge.mappings.keys import item_key
from docforge.mappings.service import Bill, ItemMappingView, SupplierMappingView
from docforge.profiles.render import Booked

GOLDEN = Path(__file__).parent / "fixtures" / "profiles" / "SAMPLE-synthetic-purchase-import.csv"
GUJARAT_SELLER = "24AABFN7754K1Z0"
MAHARASHTRA_SELLER = "27AAACS1234F1ZS"
BUYER = "24AAACD5678G1ZN"
_WHEN = datetime(2026, 10, 1, tzinfo=UTC)


def field(value: Any) -> dict[str, Any]:
    return {"value": None if value is None else str(value), "raw": None, "block_ids": []}


def line(**values: Any) -> dict[str, Any]:
    names = ("product_name", "pack", "hsn", "batch_no", "mfg", "expiry", "qty", "free_qty",
             "mrp", "ptr", "discount_pct", "taxable_value", "gst_rate", "amount")  # fmt: skip
    return {name: field(values.get(name)) for name in names}


def party(gstin: str | None, name: str) -> dict[str, Any]:
    return {
        "name": field(name),
        "address": field(None),
        "gstin": field(gstin),
        "drug_licence_nos": [field("GJ-SYN-20B-0001")],
    }


def invoice(
    *, number: str | None, day: str | None, seller: str | None, code: str | None,
    lines: list[dict[str, Any]], totals: dict[str, Any],
) -> InvoiceExtraction:  # fmt: skip
    return InvoiceExtraction.model_validate(
        {
            "invoice_no": field(number),
            "invoice_date": field(day),
            "po_no": field(None),
            "po_date": field(None),
            "place_of_supply": field(None),
            "place_of_supply_code": field(code),
            "seller": party(seller, "Synthetic Seller"),
            "buyer": party(BUYER, "Synthetic Buyer"),
            "lines": lines,
            "totals": {
                name: field(totals.get(name))
                for name in ("taxable_value", "cgst", "sgst", "igst", "round_off", "grand_total")
            },
            "issues": [],
        }
    )


def first_bill() -> InvoiceExtraction:
    return invoice(
        number="SYN/0001", day="2026-10-05", seller=GUJARAT_SELLER, code="24",
        lines=[
            line(product_name="Synthetic Paracetamol 500mg Tab", pack="10x10", hsn="30049099",
                 batch_no="SB101", mfg="2026-01", expiry="2028-12", qty=10, free_qty=2,
                 mrp="25.00", ptr="18.00", discount_pct="0", taxable_value="180.00",
                 gst_rate="12", amount="201.60"),
            line(product_name="Synthetic Cough Syrup 100ml", pack="100ml", hsn="30049011",
                 batch_no="SC202", mfg="2026-03", expiry="2027-02", qty=5, free_qty=None,
                 mrp="110.00", ptr="80.00", discount_pct="10", taxable_value="360.00",
                 gst_rate="5", amount="378.00"),
        ],
        totals={"taxable_value": "540.00", "cgst": "19.80", "sgst": "19.80",
                "round_off": "0.40", "grand_total": "580.00"},
    )  # fmt: skip


def second_bill() -> InvoiceExtraction:
    return invoice(
        number="SYN/0002", day="2026-10-06", seller=MAHARASHTRA_SELLER, code="24",
        lines=[
            line(product_name="Synthetic Amoxicillin 250mg Cap", pack="10x10", hsn="30041090",
                 batch_no="SA303", mfg="2026-05", expiry="2028-04", qty=20, free_qty=4,
                 mrp="60.00", ptr="42.50", discount_pct="0", taxable_value="850.00",
                 gst_rate="12", amount="952.00"),
        ],
        totals={"taxable_value": "850.00", "igst": "102.00", "round_off": "0.00",
                "grand_total": "952.00"},
    )  # fmt: skip


def supplier(gstin: str, marg: str | None) -> SupplierMappingView:
    return SupplierMappingView(
        id=uuid.uuid4(), seller_gstin=gstin, printed_name="Synthetic Seller", tally_ledger=None,
        marg_party=marg, purchase_ledger="Purchase GST", tax_treatment="regular",
        source="manual", created_by="Asha Rao", created_at=_WHEN, updated_by=None,
        updated_at=None,
    )  # fmt: skip


def item(name: str, pack: str, marg: str, preferred_hsn: str | None = None) -> ItemMappingView:
    return ItemMappingView(
        id=uuid.uuid4(), item_key=item_key(name, pack) or "", product_name=name, pack=pack,
        printed_hsn=None, tally_item=None, marg_item=marg, preferred_hsn=preferred_hsn,
        source="manual", created_by="Asha Rao", created_at=_WHEN, updated_by=None,
        updated_at=None,
    )  # fmt: skip


def bill(invoice: InvoiceExtraction, name: str) -> Bill:
    return Bill(uuid.uuid5(uuid.NAMESPACE_URL, name), name, invoice)


def booked() -> list[Booked]:
    """The two bills, mapped as a workspace would map them for Marg."""
    first, second = first_bill(), second_bill()
    return [
        Booked(
            bill(first, "syn-0001.pdf"),
            supplier(GUJARAT_SELLER, "SYNTHETIC SUPPLIER (AHD)"),
            (
                item("Synthetic Paracetamol 500mg Tab", "10x10", "PARACETAMOL 500 TAB SYN"),
                item("Synthetic Cough Syrup 100ml", "100ml", "COUGH SYRUP 100ML SYN"),
            ),
        ),
        Booked(
            bill(second, "syn-0002.pdf"),
            supplier(MAHARASHTRA_SELLER, "SYNTHETIC PHARMA (MUMBAI)"),
            (item("Synthetic Amoxicillin 250mg Cap", "10x10", "AMOXY 250 CAP SYN", "30041010"),),
        ),
    ]
