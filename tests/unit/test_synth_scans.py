"""Scanned variants of the synthetic invoices, with labels moved to where the ink now is."""

import json
import shutil
from pathlib import Path

import pytest

from docforge.parsing.pdf import has_text_layer, pdf_page_count
from docforge.parsing.raster import render_pages, rotate_box
from docforge.synth.__main__ import main
from docforge.synth.models import PairLabel
from docforge.synth.scans import PROFILES, generate_scans, scan_pdf

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SYNTHETIC = FIXTURES / "synthetic"
SCANNED = FIXTURES / "scanned"
INVOICE = (SYNTHETIC / "pair_001" / "invoice.pdf").read_bytes()


def test_there_is_a_good_and_a_poor_profile() -> None:
    assert set(PROFILES) == {"scan_good", "scan_poor"}
    assert PROFILES["scan_good"].angle == 0.0
    assert PROFILES["scan_poor"].angle != 0.0
    assert PROFILES["scan_poor"].dpi < PROFILES["scan_good"].dpi


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_a_scan_is_an_image_of_the_same_pages_with_no_text_layer(profile: str) -> None:
    scan = scan_pdf(INVOICE, PROFILES[profile], seed=7)

    assert not has_text_layer(scan)
    assert pdf_page_count(scan) == pdf_page_count(INVOICE)
    (original,), (scanned,) = render_pages(INVOICE, dpi=72), render_pages(scan, dpi=72)
    assert scanned.size == pytest.approx(original.size, abs=2)


def test_the_same_seed_gives_the_same_scan_and_another_seed_a_different_one() -> None:
    profile = PROFILES["scan_poor"]

    assert scan_pdf(INVOICE, profile, seed=7) == scan_pdf(INVOICE, profile, seed=7)
    assert scan_pdf(INVOICE, profile, seed=7) != scan_pdf(INVOICE, profile, seed=8)


def test_generating_writes_scans_labels_and_a_manifest(tmp_path: Path) -> None:
    written = generate_scans(SYNTHETIC, tmp_path, pairs=("pair_001", "pair_002"))

    assert written == 4
    for name, profile in PROFILES.items():
        manifest = json.loads((tmp_path / name / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["pairs"] == ["pair_001", "pair_002"]
        assert manifest["profile"] == profile.model_dump()
        assert not has_text_layer((tmp_path / name / "pair_001" / "invoice.pdf").read_bytes())


def test_label_boxes_move_with_the_rotation_of_the_page(tmp_path: Path) -> None:
    generate_scans(SYNTHETIC, tmp_path, pairs=("pair_001",))
    original = PairLabel.model_validate_json(
        (SYNTHETIC / "pair_001" / "label.json").read_text(encoding="utf-8")
    )
    boxes = original.documents["invoice"]

    for name, profile in PROFILES.items():
        label = PairLabel.model_validate_json(
            (tmp_path / name / "pair_001" / "label.json").read_text(encoding="utf-8")
        )
        assert label.invoice == original.invoice  # the values do not change
        moved = label.documents["invoice"]
        for before, after in zip(boxes.boxes, moved.boxes, strict=True):
            expected = rotate_box(
                (before.x0, before.y0, before.x1, before.y1),
                profile.angle,
                boxes.page_width,
                boxes.page_height,
            )
            assert (after.x0, after.y0, after.x1, after.y1) == pytest.approx(expected, abs=0.01)
            assert (after.path, after.text, after.page) == (before.path, before.text, before.page)


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_the_committed_scans_cover_every_synthetic_pair(profile: str) -> None:
    manifest = json.loads((SCANNED / profile / "manifest.json").read_text(encoding="utf-8"))
    synthetic = json.loads((SYNTHETIC / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["pairs"] == synthetic["pairs"]
    assert manifest["seed"] == synthetic["seed"]
    for pair in manifest["pairs"]:
        assert (SCANNED / profile / pair / "invoice.pdf").is_file()
        assert (SCANNED / profile / pair / "label.json").is_file()


def test_the_command_line_writes_scans_of_an_existing_set(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(SYNTHETIC / "pair_001", source / "pair_001")
    manifest = {"schema_version": "1", "seed": 5, "count": 1, "pairs": ["pair_001"]}
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert main(["--scans-from", str(source), "--out", str(tmp_path / "out")]) == 0

    assert (tmp_path / "out" / "scan_poor" / "pair_001" / "invoice.pdf").is_file()
