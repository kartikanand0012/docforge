"""Synthetic certificates of analysis (CoA) for batches on the synthetic invoices.

A distributor receives a CoA from the manufacturer for each batch it sells. Each certificate
here is for the batch on the first line of one invoice pair, with tests that suit the
product's form and results drawn inside their limits. Six are seeded with one result outside
its limit; three of those still say the batch complies, which is the case a checker must not
miss. The label names the seeded test.
"""

import json
import random
import shutil
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from reportlab.lib.pagesizes import A4

from docforge.synth.builder import build_pair
from docforge.synth.models import DocumentBoxes, DocumentPair, Month
from docforge.synth.render import (
    _MARGIN,
    RenderedDocument,
    _Column,
    _month,
    _Page,
    _Style,
)

# (case id, pair number, the test pushed out of limit or None, conclusion still says "complies")
COA_CASES: tuple[tuple[str, int, str | None, bool], ...] = tuple(
    (f"coa_{n:03d}", n, oos, claims)
    for n, oos, claims in (
        (1, None, True), (2, "Assay", True), (3, None, True), (4, None, True),
        (5, "Related substances", False), (6, None, True), (7, None, True),
        (8, "Assay", False), (9, None, True), (10, None, True), (11, "Related substances", True),
        (12, None, True), (13, None, True), (14, "Assay", True), (15, None, True),
        (16, None, True), (17, "Related substances", False), (18, None, True),
        (19, None, True), (20, None, True),
    )
)  # fmt: skip
MANIFEST_FILE = "manifest.json"
COA_FILE = "coa.pdf"
LABEL_FILE = "label.json"
COMPLIES = "The batch complies with the specification."
DOES_NOT_COMPLY = "The batch does not comply with the specification."

_MANUFACTURERS = (
    ("Aravali Life Sciences Ltd.", "ALS"),
    ("Kaveri Therapeutics Pvt. Ltd.", "KTP"),
    ("Sahyadri Pharmaceuticals Ltd.", "SPL"),
    ("Narmada Healthcare Ltd.", "NHL"),
)
_STYLE = _Style(page=A4, font_size=8.5, row_pitch=16.0, dates="dd-Mon-yyyy")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CoaTest(_Model):
    name: str
    specification: str
    result: str


class Coa(_Model):
    coa_no: str
    manufacturer: str
    product_name: str
    batch_no: str
    mfg: Month
    expiry: Month
    analysis_date: date
    tests: tuple[CoaTest, ...]
    conclusion: str


class CoaLabel(_Model):
    schema_version: str = "1"
    case_id: str
    pair_id: str
    seed: int
    out_of_limit: tuple[str, ...]  # tests whose result is outside the specification
    coa: Coa
    document: DocumentBoxes


def _form(product: str) -> str:
    name = product.lower()
    if "injection" in name:
        return "injection"
    if "tablet" in name or "capsule" in name:
        return "solid"
    return "other"


def _number(rng: random.Random, low: float, high: float, places: int) -> Decimal:
    return Decimal(f"{rng.uniform(low, high):.{places}f}")


def _tests(form: str, rng: random.Random, out_of_limit: str | None) -> tuple[CoaTest, ...]:
    """The tests for a form, each result inside its limit except `out_of_limit`."""

    def assay(low: float, high: float) -> CoaTest:
        spec = f"{low:.1f} - {high:.1f} %"
        value = (
            _number(rng, low - 4.0, low - 1.5, 1)
            if out_of_limit == "Assay"
            else _number(rng, low + 1.0, high - 1.0, 1)
        )
        return CoaTest(name="Assay", specification=spec, result=f"{value} %")

    def related(limit: float) -> CoaTest:
        value = (
            _number(rng, limit + 0.3, limit + 1.0, 2)
            if out_of_limit == "Related substances"
            else _number(rng, 0.05, limit * 0.6, 2)
        )
        return CoaTest(
            name="Related substances", specification=f"NMT {limit:.1f} %", result=f"{value} %"
        )

    common = [
        CoaTest(name="Description", specification="Complies", result="Complies"),
        CoaTest(name="Identification", specification="Positive", result="Positive"),
    ]
    if form == "solid":
        return (
            *common,
            CoaTest(
                name="Dissolution", specification="NLT 80 %", result=f"{_number(rng, 86, 99, 0)} %"
            ),
            assay(95.0, 105.0),
            related(1.0),
            CoaTest(
                name="Water",
                specification="NMT 5.0 % w/w",
                result=f"{_number(rng, 1.0, 3.8, 1)} % w/w",
            ),
        )
    if form == "injection":
        return (
            *common,
            CoaTest(name="pH", specification="3.5 - 5.5", result=str(_number(rng, 3.9, 5.1, 1))),
            assay(95.0, 105.0),
            related(1.0),
            CoaTest(
                name="Bacterial endotoxins",
                specification="NMT 0.5 EU/mL",
                result=f"{_number(rng, 0.05, 0.3, 2)} EU/mL",
            ),
            CoaTest(name="Sterility", specification="Sterile", result="Complies"),
        )
    return (
        *common,
        assay(90.0, 110.0),
        related(2.0),
        CoaTest(name="Microbial limits", specification="Complies", result="Complies"),
    )  # fmt: skip


def build_coa(
    pair: DocumentPair, seed: int, out_of_limit: str | None, claims_complies: bool = True
) -> Coa:
    line = pair.invoice.lines[0]
    rng = random.Random(f"{seed}:coa:{pair.pair_id}")  # noqa: S311 - reproducible test data
    manufacturer, code = rng.choice(_MANUFACTURERS)
    made = date(int(line.mfg[:4]), int(line.mfg[5:7]), 1)
    tests = _tests(_form(line.product_name), rng, out_of_limit)
    clean = out_of_limit is None
    return Coa(
        coa_no=f"COA/{code}/{made:%y}/{rng.randint(100, 999)}",
        manufacturer=manufacturer,
        product_name=line.product_name,
        batch_no=line.batch_no,
        mfg=line.mfg,
        expiry=line.expiry,
        analysis_date=made + timedelta(days=rng.randint(5, 25)),
        tests=tests,
        conclusion=COMPLIES if clean or claims_complies else DOES_NOT_COMPLY,
    )


_COLUMNS: tuple[_Column[CoaTest], ...] = (
    _Column("name", "Test", 2.2, "left", lambda t: t.name),
    _Column("specification", "Specification", 3.0, "left", lambda t: t.specification),
    _Column("result", "Result", 1.8, "left", lambda t: t.result),
)


def _dated(value: date) -> str:
    return value.strftime("%d-%b-%Y")


def render_coa(coa: Coa) -> RenderedDocument:
    page = _Page(_STYLE)
    pitch = _STYLE.font_size + 6
    y = page.top
    page.text(_MARGIN, y, "CERTIFICATE OF ANALYSIS", bold=True, size=13)
    y -= pitch + 6
    page.text(_MARGIN, y, coa.manufacturer, bold=True, path="manufacturer")
    y -= pitch + 4
    for label, value, path in (
        ("Certificate No.:", coa.coa_no, "coa_no"),
        ("Product:", coa.product_name, "product_name"),
        ("Batch No.:", coa.batch_no, "batch_no"),
        ("Mfg. Date:", _month(coa.mfg), "mfg"),
        ("Exp. Date:", _month(coa.expiry), "expiry"),
        ("Date of Analysis:", _dated(coa.analysis_date), "analysis_date"),
    ):
        page.labelled(_MARGIN, y, label, value, path)
        y -= pitch
    bottom = page.table(y - pitch, _COLUMNS, coa.tests, prefix="tests")
    y = bottom - pitch - 6
    page.text(_MARGIN, y, "Conclusion:", bold=True)
    page.text(_MARGIN + 60, y, coa.conclusion, path="conclusion")
    page.text(_MARGIN, y - 3 * pitch, "Approved by: Quality Assurance")
    return page.finish(coa)


def _label(
    case_id: str,
    pair: DocumentPair,
    seed: int,
    oos: str | None,
    coa: Coa,
    rendered: RenderedDocument,
) -> CoaLabel:
    return CoaLabel(
        case_id=case_id,
        pair_id=pair.pair_id,
        seed=seed,
        out_of_limit=(oos,) if oos else (),
        coa=coa,
        document=DocumentBoxes(
            file=COA_FILE,
            page_width=rendered.page_width,
            page_height=rendered.page_height,
            boxes=rendered.boxes,
            unprinted=rendered.unprinted,
        ),
    )


def generate_coas(out_dir: Path, seed: int) -> list[CoaLabel]:
    """Write every case of `COA_CASES` under `out_dir`, replacing earlier ones."""
    built = []
    for case_id, index, oos, claims in COA_CASES:
        pair = build_pair(index, seed)
        coa = build_coa(pair, seed, oos, claims)
        rendered = render_coa(coa)
        built.append((_label(case_id, pair, seed, oos, coa, rendered), rendered.pdf))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / MANIFEST_FILE).unlink(missing_ok=True)
    for path in out_dir.glob("coa_*"):
        if path.is_dir():
            shutil.rmtree(path)
    for label, pdf in built:
        directory = out_dir / label.case_id
        directory.mkdir()
        (directory / COA_FILE).write_bytes(pdf)
        (directory / LABEL_FILE).write_text(
            label.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    manifest = {"schema_version": "1", "seed": seed, "cases": [label.case_id for label, _ in built]}
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return [label for label, _ in built]
