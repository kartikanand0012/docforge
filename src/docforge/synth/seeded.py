"""Synthetic pairs with one known defect each, to prove the trust layer catches them.

A case is a clean pair with a single change, and a label that states what must be reported:
a failed rule, a discrepancy with the order, or a value that fails source verification.
Most defects are printed on the document. `wrong_batch` is a model error, so the document
is left correct and the label says how to corrupt the model's reply instead.
"""

import json
import re
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Literal

from docforge.synth import DEFAULT_SEED
from docforge.synth.builder import _shift_month, build_pair, compute_totals, line_amounts
from docforge.synth.dataset import INVOICE_FILE, LABEL_FILE, MANIFEST_FILE, ORDER_FILE, _boxes
from docforge.synth.models import (
    DocumentPair,
    Invoice,
    InvoiceLine,
    PairLabel,
    PurchaseOrder,
    _Model,
)
from docforge.synth.render import render_invoice, render_purchase_order

DEFECTS = (
    "bad_line_amount",
    "bad_grand_total",
    "expired_stock",
    "ptr_above_mrp",
    "bad_gstin",
    "qty_over_order",
    "rate_over_order",
    "free_qty_short",
    "wrong_batch",
)
SEEDED_COUNT = len(DEFECTS)
_FIRST_INDEX = 100  # keeps seeded pairs distinct from the clean set built from the same seed
_CASE_DIR = re.compile(r"case_\d{3}")


class ExpectedFinding(_Model):
    kind: Literal["rule", "discrepancy", "verification"]
    code: str  # the rule id, the discrepancy code, or the field path that must be flagged


class CorruptReply(_Model):
    """A model error to simulate: replace the text the model returns for one field."""

    path: str
    text: str


class SeededLabel(PairLabel):
    defect: str
    expected: tuple[ExpectedFinding, ...]
    corrupt_reply: CorruptReply | None = None


def _rule(code: str) -> ExpectedFinding:
    return ExpectedFinding(kind="rule", code=code)


def _discrepancy(code: str) -> ExpectedFinding:
    return ExpectedFinding(kind="discrepancy", code=code)


def _with_line(invoice: Invoice, line: InvoiceLine, *, retotal: bool) -> Invoice:
    """Replace the first line; with `retotal`, keep the invoice's own arithmetic consistent."""
    lines = (line, *invoice.lines[1:])
    totals = compute_totals(lines) if retotal else invoice.totals
    return invoice.model_copy(update={"lines": lines, "totals": totals})


def _repriced(invoice: Invoice, *, qty: int | None = None, ptr: Decimal | None = None) -> Invoice:
    """The first line with a new quantity or rate, and every amount recomputed to agree."""
    line = invoice.lines[0]
    qty, ptr = qty or line.qty, ptr or line.ptr
    amounts = line_amounts(
        qty=qty,
        ptr=ptr,
        discount_pct=line.discount_pct,
        gst_rate=line.gst_rate,
        intra_state=invoice.supply_type == "intra_state",
    )
    changed = line.model_copy(update={"qty": qty, "ptr": ptr, **amounts._asdict()})
    return _with_line(invoice, changed, retotal=True)


def _apply(
    defect: str, invoice: Invoice, order: PurchaseOrder
) -> tuple[Invoice, PurchaseOrder, tuple[ExpectedFinding, ...], CorruptReply | None]:
    line = invoice.lines[0]
    if defect == "bad_line_amount":
        wrong = line.model_copy(update={"amount": line.amount + Decimal("10.00")})
        expected: tuple[ExpectedFinding, ...] = (_rule("line.amount"), _rule("totals.tax"))
        return _with_line(invoice, wrong, retotal=False), order, expected, None
    if defect == "bad_grand_total":
        totals = invoice.totals.model_copy(
            update={"grand_total": invoice.totals.grand_total + Decimal("100.00")}
        )
        changed = invoice.model_copy(update={"totals": totals})
        return changed, order, (_rule("totals.grand_total"),), None
    if defect == "expired_stock":
        issued = invoice.invoice_date
        expired = line.model_copy(
            update={
                "expiry": _shift_month(issued.year, issued.month, -1),
                "mfg": _shift_month(issued.year, issued.month, -25),
            }
        )
        expected = (_rule("line.not_expired"),)
        return _with_line(invoice, expired, retotal=False), order, expected, None
    if defect == "ptr_above_mrp":
        changed = _repriced(invoice, ptr=line.mrp + Decimal("5.00"))
        return changed, order, (_rule("line.ptr_not_above_mrp"), _discrepancy("line.rate")), None
    if defect == "bad_gstin":
        gstin = invoice.seller.gstin
        wrong_check = gstin[:-1] + ("A" if gstin[-1] != "A" else "B")
        seller = invoice.seller.model_copy(update={"gstin": wrong_check})
        changed = invoice.model_copy(update={"seller": seller})
        return changed, order, (_rule("gstin.checksum"), _discrepancy("supplier.gstin")), None
    if defect == "qty_over_order":
        # One more than ordered, with no free goods either way so only the quantity differs.
        lines = (order.lines[0].model_copy(update={"scheme": None}), *order.lines[1:])
        changed = _repriced(invoice, qty=line.qty + 1)
        first = changed.lines[0].model_copy(update={"free_qty": 0})
        changed = _with_line(changed, first, retotal=False)
        ordered = order.model_copy(update={"lines": lines})
        return changed, ordered, (_discrepancy("line.qty"),), None
    if defect == "rate_over_order":
        changed = _repriced(invoice, ptr=line.ptr + Decimal("1.00"))
        return changed, order, (_discrepancy("line.rate"),), None
    if defect == "free_qty_short":
        lines = (order.lines[0].model_copy(update={"scheme": "10+1"}), *order.lines[1:])
        short = line.model_copy(update={"free_qty": 0})
        ordered = order.model_copy(update={"lines": lines})
        expected = (_discrepancy("line.free_qty"),)
        return _with_line(invoice, short, retotal=False), ordered, expected, None
    if defect == "wrong_batch":
        batch = line.batch_no
        misread = batch[:-2] + batch[-1] + batch[-2]
        if misread == batch:
            misread = batch[:-1] + ("7" if batch[-1] != "7" else "1")
        corrupt = CorruptReply(path="lines[0].batch_no", text=misread)
        expected = (ExpectedFinding(kind="verification", code="lines[0].batch_no"),)
        return invoice, order, expected, corrupt
    raise ValueError(f"unknown defect {defect!r}")


def build_seeded_case(number: int, seed: int = DEFAULT_SEED) -> tuple[SeededLabel, bytes, bytes]:
    """Case `number` (1-based): its label, the invoice PDF and the purchase-order PDF."""
    if not 1 <= number <= SEEDED_COUNT:
        raise ValueError(f"number must be between 1 and {SEEDED_COUNT}")
    defect = DEFECTS[number - 1]
    clean: DocumentPair = build_pair(_FIRST_INDEX + number, seed)
    invoice, order, expected, corrupt = _apply(defect, clean.invoice, clean.purchase_order)
    invoice_pdf = render_invoice(invoice, clean.layout)
    order_pdf = render_purchase_order(order, clean.layout)
    label = SeededLabel(
        pair_id=f"case_{number:03d}",
        seed=seed,
        layout=clean.layout,
        invoice=invoice,
        purchase_order=order,
        documents={
            "invoice": _boxes(INVOICE_FILE, invoice_pdf),
            "purchase_order": _boxes(ORDER_FILE, order_pdf),
        },
        defect=defect,
        expected=expected,
        corrupt_reply=corrupt,
    )
    return label, invoice_pdf.pdf, order_pdf.pdf


def generate_seeded(out_dir: Path, seed: int = DEFAULT_SEED) -> list[SeededLabel]:
    """Write every seeded case to `out_dir`, replacing cases already there."""
    built = [build_seeded_case(number, seed) for number in range(1, SEEDED_COUNT + 1)]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / MANIFEST_FILE).unlink(missing_ok=True)
    for path in out_dir.iterdir():
        if path.is_dir() and not path.is_symlink() and _CASE_DIR.fullmatch(path.name):
            shutil.rmtree(path)
    for label, invoice_pdf, order_pdf in built:
        case_dir = out_dir / label.pair_id
        case_dir.mkdir()
        (case_dir / INVOICE_FILE).write_bytes(invoice_pdf)
        (case_dir / ORDER_FILE).write_bytes(order_pdf)
        (case_dir / LABEL_FILE).write_text(label.model_dump_json(indent=2) + "\n", encoding="utf-8")
    labels = [label for label, _, _ in built]
    manifest = {
        "schema_version": "1",
        "seed": seed,
        "count": len(labels),
        "pairs": [label.pair_id for label in labels],
    }
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return labels
