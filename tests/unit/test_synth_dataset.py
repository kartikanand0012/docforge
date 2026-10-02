"""Writing the dataset to disk, and the committed fixtures staying reproducible."""

import json
from pathlib import Path

import pytest

from docforge.synth import DEFAULT_COUNT, DEFAULT_SEED, dataset
from docforge.synth.__main__ import main
from docforge.synth.dataset import generate_dataset
from docforge.synth.models import Invoice, Layout, PairLabel, leaf_paths
from docforge.synth.render import RenderedDocument, render_invoice

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PAIR_FILES = {"invoice.pdf", "purchase_order.pdf", "label.json"}


def pair_dirs(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir() if path.is_dir())


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_writes_one_directory_per_pair(tmp_path: Path) -> None:
    labels = generate_dataset(tmp_path, count=3, seed=DEFAULT_SEED)

    assert [label.pair_id for label in labels] == ["pair_001", "pair_002", "pair_003"]
    assert [path.name for path in pair_dirs(tmp_path)] == ["pair_001", "pair_002", "pair_003"]
    for directory in pair_dirs(tmp_path):
        assert {path.name for path in directory.iterdir()} == PAIR_FILES


def test_label_file_round_trips_through_the_schema(tmp_path: Path) -> None:
    (label,) = generate_dataset(tmp_path, count=1, seed=DEFAULT_SEED)

    on_disk = PairLabel.model_validate_json(
        (tmp_path / "pair_001" / "label.json").read_text(encoding="utf-8")
    )

    assert on_disk == label
    assert on_disk.seed == DEFAULT_SEED
    assert on_disk.documents["invoice"].file == "invoice.pdf"
    assert on_disk.documents["purchase_order"].file == "purchase_order.pdf"
    assert on_disk.documents["invoice"].boxes


def test_label_accounts_for_every_value_as_boxed_or_unprinted(tmp_path: Path) -> None:
    (label,) = generate_dataset(tmp_path, count=1, seed=DEFAULT_SEED)

    for name, truth in (("invoice", label.invoice), ("purchase_order", label.purchase_order)):
        document = label.documents[name]
        boxed = {box.path for box in document.boxes}
        unprinted = set(document.unprinted)

        assert not boxed & unprinted
        assert boxed | unprinted == leaf_paths(truth)


def test_label_money_is_serialised_as_two_place_strings(tmp_path: Path) -> None:
    generate_dataset(tmp_path, count=1, seed=DEFAULT_SEED)

    raw = json.loads((tmp_path / "pair_001" / "label.json").read_text(encoding="utf-8"))

    grand_total = raw["invoice"]["totals"]["grand_total"]
    assert isinstance(grand_total, str)
    assert grand_total.endswith(".00")
    assert isinstance(raw["invoice"]["lines"][0]["qty"], int)


def test_label_rejects_money_with_more_than_two_places(tmp_path: Path) -> None:
    generate_dataset(tmp_path, count=1, seed=DEFAULT_SEED)
    raw = json.loads((tmp_path / "pair_001" / "label.json").read_text(encoding="utf-8"))
    raw["invoice"]["totals"]["grand_total"] = "100.005"

    with pytest.raises(ValueError, match="decimal places"):
        PairLabel.model_validate(raw)


def test_manifest_lists_every_pair(tmp_path: Path) -> None:
    generate_dataset(tmp_path, count=2, seed=7)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

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
    (keep / "readme.txt").write_text("keep me", encoding="utf-8")

    generate_dataset(tmp_path, count=1, seed=DEFAULT_SEED)

    assert (keep / "readme.txt").read_text(encoding="utf-8") == "keep me"


def test_a_failed_run_leaves_the_existing_set_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate_dataset(tmp_path, count=3, seed=DEFAULT_SEED)
    before = snapshot(tmp_path)
    calls = 0

    def fail_on_second_invoice(invoice: Invoice, layout: Layout) -> RenderedDocument:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("render failed")
        return render_invoice(invoice, layout)

    monkeypatch.setattr(dataset, "render_invoice", fail_on_second_invoice)

    with pytest.raises(RuntimeError, match="render failed"):
        generate_dataset(tmp_path, count=3, seed=DEFAULT_SEED + 1)

    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("count", [0, -1, 1000])
def test_count_must_be_within_range(tmp_path: Path, count: int) -> None:
    with pytest.raises(ValueError, match="count"):
        generate_dataset(tmp_path, count=count, seed=DEFAULT_SEED)


def test_cli_generates_into_the_given_directory(tmp_path: Path) -> None:
    exit_code = main(["--count", "2", "--seed", "11", "--out", str(tmp_path / "out")])

    assert exit_code == 0
    assert len(pair_dirs(tmp_path / "out")) == 2


def test_cli_reports_a_bad_count_as_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--count", "0", "--out", str(tmp_path / "out")])

    assert exit_info.value.code == 2
    assert "count must be between" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_committed_fixtures_hold_the_full_set() -> None:
    directories = pair_dirs(FIXTURES)

    assert len(directories) == DEFAULT_COUNT == 20
    for directory in directories:
        assert {path.name for path in directory.iterdir()} == PAIR_FILES


def test_committed_fixtures_match_a_fresh_generation_byte_for_byte(tmp_path: Path) -> None:
    """If this fails after a dependency bump, run `make generate` and review the diff."""
    generate_dataset(tmp_path, count=DEFAULT_COUNT, seed=DEFAULT_SEED)

    fresh, committed = snapshot(tmp_path), snapshot(FIXTURES)

    assert fresh.keys() == committed.keys()
    differing = [name for name in fresh if fresh[name] != committed[name]]
    assert differing == []
