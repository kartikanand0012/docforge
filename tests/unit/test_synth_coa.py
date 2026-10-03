"""Synthetic certificates of analysis, some with a result outside its limit."""

import io
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from docforge.synth import DEFAULT_SEED
from docforge.synth.builder import build_pair
from docforge.synth.coa import COA_CASES, build_coa, generate_coas, render_coa
from docforge.trust.limits import check_result

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "coa"


def test_a_certificate_is_for_the_batch_on_the_invoice() -> None:
    pair = build_pair(1, DEFAULT_SEED)
    line = pair.invoice.lines[0]

    coa = build_coa(pair, seed=DEFAULT_SEED, out_of_limit=None)

    assert (coa.product_name, coa.batch_no) == (line.product_name, line.batch_no)
    assert (coa.mfg, coa.expiry) == (line.mfg, line.expiry)
    assert len(coa.tests) >= 4


def test_a_clean_certificate_passes_every_test() -> None:
    for index in range(1, 21):
        coa = build_coa(build_pair(index, DEFAULT_SEED), seed=DEFAULT_SEED, out_of_limit=None)
        assert {check_result(t.specification, t.result) for t in coa.tests} == {"passed"}, index
        assert "complies with" in coa.conclusion


@pytest.mark.parametrize("test_name", ["Assay", "Related substances"])
@pytest.mark.parametrize("index", [3, 5, 8])  # a capsule, a tablet and another form among them
def test_a_seeded_certificate_has_exactly_the_named_test_out_of_limit(
    index: int, test_name: str
) -> None:
    seeded = build_coa(build_pair(index, DEFAULT_SEED), seed=DEFAULT_SEED, out_of_limit=test_name)

    outcomes = {t.name: check_result(t.specification, t.result) for t in seeded.tests}
    assert outcomes[test_name] == "failed"
    assert [n for n, o in outcomes.items() if o == "failed"] == [test_name]


def test_the_rendered_certificate_prints_every_labelled_value_where_its_box_says() -> None:
    pair = build_pair(2, DEFAULT_SEED)
    rendered = render_coa(build_coa(pair, seed=DEFAULT_SEED, out_of_limit=None))
    page = PdfReader(io.BytesIO(rendered.pdf)).pages[0]
    runs: list[tuple[str, float, float]] = []
    page.extract_text(
        visitor_text=lambda t, cm, tm, f, s: (
            runs.append((t.strip(), tm[4], tm[5])) if t.strip() else None
        )
    )

    paths = {box.path for box in rendered.boxes}
    assert {
        "product_name",
        "batch_no",
        "tests[0].result",
        "tests[0].specification",
        "conclusion",
    } <= paths
    for box in rendered.boxes:
        assert any(
            text == box.text and box.x0 - 0.5 <= x <= box.x1 and box.y0 - 0.5 <= y <= box.y1
            for text, x, y in runs
        ), box.path


def test_the_set_is_written_with_labels_that_name_the_seeded_defects(tmp_path: Path) -> None:
    labels = generate_coas(tmp_path, seed=DEFAULT_SEED)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["cases"] == [label.case_id for label in labels] == [c[0] for c in COA_CASES]
    seeded = [label for label in labels if label.out_of_limit]
    assert len(seeded) == 6
    assert (
        sum("complies with" in label.coa.conclusion for label in seeded) == 3
    )  # the dangerous ones


@pytest.mark.parametrize("name", ["coa.pdf", "label.json"])
def test_the_committed_set_matches_a_fresh_generation(tmp_path: Path, name: str) -> None:
    generate_coas(tmp_path, seed=DEFAULT_SEED)

    for case_id, *_ in COA_CASES:
        assert (tmp_path / case_id / name).read_bytes() == (FIXTURES / case_id / name).read_bytes()
