"""Writing the dataset to disk, and the committed fixtures staying reproducible."""

import io
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from docforge.synth import DEFAULT_COUNT, DEFAULT_SEED
from docforge.synth.__main__ import main
from docforge.synth.dataset import generate_dataset
from docforge.synth.models import PairLabel

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PAIR_FILES = {"invoice.pdf", "purchase_order.pdf", "label.json"}


def pdf_text(path: Path) -> str:
    return PdfReader(io.BytesIO(path.read_bytes())).pages[0].extract_text()


def pair_dirs(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir() if path.is_dir())


def test_writes_one_directory_per_pair(tmp_path: Path) -> None:
    labels = generate_dataset(tmp_path, count=3, seed=DEFAULT_SEED)

    assert [label.pair_id for label in labels] == ["pair_001", "pair_002", "pair_003"]
    assert [path.name for path in pair_dirs(tmp_path)] == ["pair_001", "pair_002", "pair_003"]
    for directory in pair_dirs(tmp_path):
        assert {path.name for path in directory.iterdir()} == PAIR_FILES


def test_label_file_round_trips_through_the_schema(tmp_path: Path) -> None:
    (label,) = generate_dataset(tmp_path, count=1, seed=DEFAULT_SEED)

    on_disk = PairLabel.model_validate_json((tmp_path / "pair_001" / "label.json").read_text())

    assert on_disk == label
    assert on_disk.seed == DEFAULT_SEED
    assert on_disk.documents["invoice"].file == "invoice.pdf"
    assert on_disk.documents["purchase_order"].file == "purchase_order.pdf"
    assert on_disk.documents["invoice"].boxes


def test_label_money_is_serialised_as_two_place_strings(tmp_path: Path) -> None:
    generate_dataset(tmp_path, count=1, seed=DEFAULT_SEED)

    raw = json.loads((tmp_path / "pair_001" / "label.json").read_text())

    grand_total = raw["invoice"]["totals"]["grand_total"]
    assert isinstance(grand_total, str)
    assert grand_total.endswith(".00")
    assert isinstance(raw["invoice"]["lines"][0]["qty"], int)


def test_manifest_lists_every_pair(tmp_path: Path) -> None:
    generate_dataset(tmp_path, count=2, seed=7)

    manifest = json.loads((tmp_path / "manifest.json").read_text())

    assert manifest["seed"] == 7
    assert manifest["count"] == 2
    assert manifest["pairs"] == ["pair_001", "pair_002"]


def test_regenerating_with_fewer_pairs_removes_stale_ones(tmp_path: Path) -> None:
    generate_dataset(tmp_path, count=3, seed=DEFAULT_SEED)
    generate_dataset(tmp_path, count=1, seed=DEFAULT_SEED)

    assert [path.name for path in pair_dirs(tmp_path)] == ["pair_001"]


def test_unrelated_files_in_the_output_directory_are_left_alone(tmp_path: Path) -> None:
    keep = tmp_path / "notes"
    keep.mkdir()
    (keep / "readme.txt").write_text("keep me")

    generate_dataset(tmp_path, count=1, seed=DEFAULT_SEED)

    assert (keep / "readme.txt").read_text() == "keep me"


def test_count_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="count"):
        generate_dataset(tmp_path, count=0, seed=DEFAULT_SEED)


def test_cli_generates_into_the_given_directory(tmp_path: Path) -> None:
    exit_code = main(["--count", "2", "--seed", "11", "--out", str(tmp_path / "out")])

    assert exit_code == 0
    assert len(pair_dirs(tmp_path / "out")) == 2


def test_committed_fixtures_hold_the_full_set() -> None:
    directories = pair_dirs(FIXTURES)

    assert len(directories) == DEFAULT_COUNT == 20
    for directory in directories:
        assert {path.name for path in directory.iterdir()} == PAIR_FILES


def test_committed_fixtures_match_a_fresh_generation(tmp_path: Path) -> None:
    generate_dataset(tmp_path, count=DEFAULT_COUNT, seed=DEFAULT_SEED)

    for fresh in pair_dirs(tmp_path):
        committed = FIXTURES / fresh.name
        assert (committed / "label.json").read_text() == (fresh / "label.json").read_text()
        for name in ("invoice.pdf", "purchase_order.pdf"):
            assert pdf_text(committed / name) == pdf_text(fresh / name), f"{fresh.name}/{name}"
