"""Build one internally consistent invoice / purchase-order pair from a seed."""

import random
import string
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

from docforge.gstin import make_gstin
from docforge.synth import catalog
from docforge.synth.models import (
    DocumentPair,
    Invoice,
    InvoiceLine,
    InvoiceTotals,
    Layout,
    Party,
    PurchaseOrder,
    PurchaseOrderLine,
)

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
_FIRST_INVOICE_DATE = date(2026, 4, 1)
_FINANCIAL_YEAR = "26-27"


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _shift_month(year: int, month: int, delta: int) -> str:
    months = year * 12 + (month - 1) + delta
    return f"{months // 12:04d}-{months % 12 + 1:02d}"


def _party(company: catalog.Company, licence_forms: tuple[str, str]) -> Party:
    abbr, serial = company.state.abbr, company.licence_serial
    return Party(
        name=company.name,
        address=company.address,
        state=company.state.name,
        state_code=company.state.code,
        gstin=make_gstin(company.state.code, company.pan),
        drug_licence_nos=(
            f"{abbr}-{licence_forms[0]}-{serial}",
            f"{abbr}-{licence_forms[1]}-{serial + 1}",
        ),
    )


def _pick_buyer(rng: random.Random, seller: catalog.Company, inter_state: bool) -> catalog.Company:
    candidates = [buyer for buyer in catalog.BUYERS if (buyer.state != seller.state) == inter_state]
    return rng.choice(candidates)


def _batch_no(rng: random.Random, used: set[str]) -> str:
    while True:
        letters = "".join(rng.choices(string.ascii_uppercase, k=rng.randint(2, 3)))
        digits = "".join(rng.choices(string.digits, k=rng.randint(4, 6)))
        batch = letters + digits
        if batch not in used:
            used.add(batch)
            return batch


def _free_qty(qty: int, scheme: str | None) -> int:
    if scheme is None:
        return 0
    buy, free = (int(part) for part in scheme.split("+"))
    return (qty // buy) * free


class LineAmounts(NamedTuple):
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    amount: Decimal


def line_amounts(
    *, qty: int, ptr: Decimal, discount_pct: Decimal, gst_rate: Decimal, intra_state: bool
) -> LineAmounts:
    """Money columns of one invoice line, each rounded half-up to the paisa.

    Within a state the tax is split into CGST and SGST and each half is rounded on its
    own, so their sum can be a paisa more than the rate applied to the taxable value.
    """
    taxable = _money(qty * ptr * (Decimal(100) - discount_pct) / Decimal(100))
    if intra_state:
        cgst = sgst = _money(taxable * gst_rate / Decimal(200))
        igst = _ZERO
    else:
        cgst = sgst = _ZERO
        igst = _money(taxable * gst_rate / Decimal(100))
    return LineAmounts(taxable, cgst, sgst, igst, taxable + cgst + sgst + igst)


def _line(
    rng: random.Random,
    sl_no: int,
    product: catalog.Product,
    invoice_date: date,
    intra_state: bool,
    used_batches: set[str],
) -> tuple[InvoiceLine, str | None]:
    qty = rng.choice(catalog.QUANTITIES)
    scheme = rng.choice(catalog.SCHEMES)
    mrp = _money(product.mrp * Decimal(rng.randint(95, 110)) / Decimal(100))
    ptr = _money(mrp * Decimal(rng.randint(68, 76)) / Decimal(100))
    discount_pct = rng.choice(catalog.DISCOUNTS)

    amounts = line_amounts(
        qty=qty,
        ptr=ptr,
        discount_pct=discount_pct,
        gst_rate=product.gst_rate,
        intra_state=intra_state,
    )

    mfg_offset = -rng.randint(2, 14)
    shelf_life = rng.choice((18, 24, 36))
    line = InvoiceLine(
        sl_no=sl_no,
        product_name=product.name,
        pack=product.pack,
        hsn=product.hsn,
        batch_no=_batch_no(rng, used_batches),
        mfg=_shift_month(invoice_date.year, invoice_date.month, mfg_offset),
        expiry=_shift_month(invoice_date.year, invoice_date.month, mfg_offset + shelf_life),
        qty=qty,
        free_qty=_free_qty(qty, scheme),
        mrp=mrp,
        ptr=ptr,
        discount_pct=discount_pct,
        gst_rate=product.gst_rate,
        **amounts._asdict(),
    )
    return line, scheme


def compute_totals(lines: Sequence[InvoiceLine]) -> InvoiceTotals:
    """Sum the lines and round the grand total half-up to the whole rupee."""
    taxable = sum((line.taxable_value for line in lines), _ZERO)
    cgst = sum((line.cgst for line in lines), _ZERO)
    sgst = sum((line.sgst for line in lines), _ZERO)
    igst = sum((line.igst for line in lines), _ZERO)
    unrounded = taxable + cgst + sgst + igst
    grand_total = unrounded.quantize(Decimal(1), rounding=ROUND_HALF_UP).quantize(_CENT)
    return InvoiceTotals(
        taxable_value=taxable,
        cgst=cgst,
        sgst=sgst,
        igst=igst,
        round_off=grand_total - unrounded,
        grand_total=grand_total,
    )


def build_pair(index: int, seed: int) -> DocumentPair:
    """Pair number `index` (1-based) of the set generated from `seed`.

    The same `(index, seed)` always gives the same pair. Layout alternates and every
    third pair is inter-state, so any run of six pairs covers both layouts and both
    supply types.
    """
    if index < 1:
        raise ValueError("index must be 1 or greater")
    rng = random.Random(f"{seed}:{index}")  # noqa: S311 - reproducible test data, not security
    layout: Layout = "A" if index % 2 else "B"
    inter_state = index % 3 == 0

    seller_company = rng.choice(catalog.SELLERS)
    buyer_company = _pick_buyer(rng, seller_company, inter_state)
    seller = _party(seller_company, ("20B", "21B"))
    buyer = _party(buyer_company, ("20", "21"))

    invoice_date = _FIRST_INVOICE_DATE + timedelta(days=rng.randint(0, 170))
    po_date = invoice_date - timedelta(days=rng.randint(1, 9))
    # `index` in the low digits keeps numbers unique across a set.
    invoice_no = f"{seller_company.code}/{_FINANCIAL_YEAR}/{rng.randint(1, 40):02d}{index:03d}"
    po_no = f"PO-{buyer_company.code}-{rng.randint(10, 99)}{index:03d}"

    products = rng.sample(catalog.PRODUCTS, rng.randint(3, 10))
    used_batches: set[str] = set()
    lines: list[InvoiceLine] = []
    order_lines: list[PurchaseOrderLine] = []
    for sl_no, product in enumerate(products, start=1):
        line, scheme = _line(rng, sl_no, product, invoice_date, not inter_state, used_batches)
        lines.append(line)
        order_lines.append(
            PurchaseOrderLine(
                sl_no=sl_no,
                product_name=line.product_name,
                pack=line.pack,
                qty=line.qty,
                scheme=scheme,
                rate=line.ptr,
            )
        )

    invoice = Invoice(
        invoice_no=invoice_no,
        invoice_date=invoice_date,
        po_no=po_no,
        po_date=po_date,
        seller=seller,
        buyer=buyer,
        place_of_supply=buyer.state,
        place_of_supply_code=buyer.state_code,
        supply_type="inter_state" if inter_state else "intra_state",
        lines=tuple(lines),
        totals=compute_totals(lines),
    )
    purchase_order = PurchaseOrder(
        po_no=po_no,
        po_date=po_date,
        buyer=buyer,
        supplier_name=seller.name,
        supplier_gstin=seller.gstin,
        lines=tuple(order_lines),
    )
    return DocumentPair(
        pair_id=f"pair_{index:03d}", layout=layout, invoice=invoice, purchase_order=purchase_order
    )
