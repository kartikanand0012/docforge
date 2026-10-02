"""Scanned variants of the synthetic invoices: the same pages as images, degraded.

A scan has no text layer, so it exercises the OCR path. The label keeps the same values;
its boxes are moved to where the ink is after the page was turned. The scans are committed
because the recorded parses are keyed by their bytes, and an image encoder is not guaranteed
to produce the same bytes on another machine or library version.
"""

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from pydantic import BaseModel, ConfigDict

from docforge.parsing.raster import images_to_pdf, render_pages, rotate_box
from docforge.synth.dataset import INVOICE_FILE, LABEL_FILE, MANIFEST_FILE
from docforge.synth.models import PairLabel


class ScanProfile(BaseModel):
    """How a page is degraded. `angle` is in degrees, counter-clockwise."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dpi: int
    angle: float
    blur: float  # Gaussian radius in pixels
    noise: float  # standard deviation of grey-level noise
    quality: int  # JPEG quality


PROFILES: dict[str, ScanProfile] = {
    # A flatbed scan at office settings.
    "scan_good": ScanProfile(dpi=150, angle=0.0, blur=0.0, noise=3.0, quality=60),
    # A fax-like or phone-photo quality scan, fed in slightly crooked.
    "scan_poor": ScanProfile(dpi=110, angle=1.8, blur=0.6, noise=8.0, quality=45),
}


def scan_pdf(pdf: bytes, profile: ScanProfile, seed: int) -> bytes:
    """`pdf` as page images degraded by `profile`. The same inputs give the same bytes."""
    random = np.random.default_rng(seed)
    pages: list[Image.Image] = []
    for page in render_pages(pdf, profile.dpi):
        image = page
        if profile.angle:
            image = image.rotate(profile.angle, resample=Image.Resampling.BICUBIC, fillcolor=255)
        if profile.blur:
            image = image.filter(ImageFilter.GaussianBlur(profile.blur))
        if profile.noise:
            grey = np.asarray(image, dtype=np.float64)
            noisy = grey + random.normal(0.0, profile.noise, grey.shape)
            image = Image.fromarray(np.clip(noisy, 0, 255).astype(np.uint8))
        pages.append(image)
    return images_to_pdf(pages, profile.dpi, profile.quality)


def _moved(label: PairLabel, angle: float) -> PairLabel:
    """`label` with its invoice boxes where the ink is after the page was turned."""
    boxes = label.documents["invoice"]
    moved = []
    for box in boxes.boxes:
        x0, y0, x1, y1 = rotate_box(
            (box.x0, box.y0, box.x1, box.y1), angle, boxes.page_width, boxes.page_height
        )
        moved.append(
            box.model_copy(
                update={
                    "x0": round(x0, 2),
                    "y0": round(y0, 2),
                    "x1": round(x1, 2),
                    "y1": round(y1, 2),
                }
            )
        )
    documents = {**label.documents, "invoice": boxes.model_copy(update={"boxes": tuple(moved)})}
    return label.model_copy(update={"documents": documents})


def generate_scans(source: Path, out_dir: Path, pairs: tuple[str, ...] | None = None) -> int:
    """Write `<out_dir>/<profile>/<pair>/` for every profile from the pairs under `source`.

    Returns the number of scans written. Existing files with the same names are replaced.
    """
    manifest = json.loads((source / MANIFEST_FILE).read_text(encoding="utf-8"))
    chosen = [pair for pair in manifest["pairs"] if pairs is None or pair in pairs]
    written = 0
    for name, profile in PROFILES.items():
        for position, pair in enumerate(chosen):
            label = PairLabel.model_validate_json(
                (source / pair / LABEL_FILE).read_text(encoding="utf-8")
            )
            scan = scan_pdf(
                (source / pair / INVOICE_FILE).read_bytes(), profile, manifest["seed"] + position
            )
            directory = out_dir / name / pair
            directory.mkdir(parents=True, exist_ok=True)
            (directory / INVOICE_FILE).write_bytes(scan)
            (directory / LABEL_FILE).write_text(
                _moved(label, profile.angle).model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            written += 1
        scans_manifest = {
            "schema_version": "1",
            "seed": manifest["seed"],
            "count": len(chosen),
            "pairs": chosen,
            "profile": profile.model_dump(),
        }
        (out_dir / name / MANIFEST_FILE).write_text(
            json.dumps(scans_manifest, indent=2) + "\n", encoding="utf-8"
        )
    return written
