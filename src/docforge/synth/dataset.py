"""Write a set of synthetic pairs to disk: two PDFs and a ground-truth label per pair."""

import json
import re
import shutil
from pathlib import Path

from docforge.synth import DEFAULT_COUNT, DEFAULT_SEED
from docforge.synth.builder import build_pair
from docforge.synth.models import DocumentBoxes, PairLabel
from docforge.synth.render import RenderedDocument, render_invoice, render_purchase_order

INVOICE_FILE = "invoice.pdf"
ORDER_FILE = "purchase_order.pdf"
LABEL_FILE = "label.json"
MANIFEST_FILE = "manifest.json"
MAX_COUNT = 999  # pair ids are three digits

_PAIR_DIR = re.compile(r"pair_\d{3}")


def _boxes(file: str, document: RenderedDocument) -> DocumentBoxes:
    return DocumentBoxes(
        file=file,
        page_width=document.page_width,
        page_height=document.page_height,
        boxes=document.boxes,
        unprinted=document.unprinted,
    )


def _remove_previous_pairs(out_dir: Path) -> None:
    """Delete only directories this generator created, so a smaller set leaves no strays."""
    for path in out_dir.iterdir():
        if path.is_dir() and not path.is_symlink() and _PAIR_DIR.fullmatch(path.name):
            shutil.rmtree(path)


def _build(index: int, seed: int) -> tuple[PairLabel, bytes, bytes]:
    pair = build_pair(index, seed)
    invoice = render_invoice(pair.invoice, pair.layout)
    order = render_purchase_order(pair.purchase_order, pair.layout)
    label = PairLabel(
        pair_id=pair.pair_id,
        seed=seed,
        layout=pair.layout,
        invoice=pair.invoice,
        purchase_order=pair.purchase_order,
        documents={
            "invoice": _boxes(INVOICE_FILE, invoice),
            "purchase_order": _boxes(ORDER_FILE, order),
        },
    )
    return label, invoice.pdf, order.pdf


def generate_dataset(
    out_dir: Path, count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED
) -> list[PairLabel]:
    """Generate `count` pairs into `out_dir`, replacing any pairs already there.

    Everything is built in memory before the directory is touched, so a failure while
    building leaves an existing set exactly as it was.
    """
    if not 1 <= count <= MAX_COUNT:
        raise ValueError(f"count must be between 1 and {MAX_COUNT}")
    built = [_build(index, seed) for index in range(1, count + 1)]

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / MANIFEST_FILE).unlink(missing_ok=True)  # written last, so its presence means done
    _remove_previous_pairs(out_dir)
    for label, invoice_pdf, order_pdf in built:
        pair_dir = out_dir / label.pair_id
        pair_dir.mkdir()
        (pair_dir / INVOICE_FILE).write_bytes(invoice_pdf)
        (pair_dir / ORDER_FILE).write_bytes(order_pdf)
        (pair_dir / LABEL_FILE).write_text(label.model_dump_json(indent=2) + "\n", encoding="utf-8")

    labels = [label for label, _, _ in built]
    manifest = {
        "schema_version": "1",
        "seed": seed,
        "count": count,
        "pairs": [label.pair_id for label in labels],
    }
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return labels
