"""Score one extraction against its label, field by field.

Only values printed on the page are scored (the label's boxes). A numeric or date field is
correct when the typed values are equal, however they were written. Tax components that do
not apply to the invoice are not printed; the extraction must leave them null.
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from docforge.extraction.schema import Extracted, InvoiceExtraction
from docforge.parsing.base import ParsedDocument
from docforge.synth.models import FieldBox, Layout, PairLabel

FieldClass = Literal["identifier", "date", "amount", "quantity", "text"]
Outcome = Literal["correct", "wrong", "missing", "correct_null", "hallucinated"]

_CLASS_MEMBERS: dict[FieldClass, tuple[str, ...]] = {
    "identifier": (
        "invoice_no",
        "po_no",
        "gstin",
        "drug_licence_nos",
        "batch_no",
        "hsn",
        "place_of_supply_code",
    ),
    "date": ("invoice_date", "po_date", "mfg", "expiry"),
    "amount": (
        "mrp",
        "ptr",
        "taxable_value",
        "amount",
        "cgst",
        "sgst",
        "igst",
        "round_off",
        "grand_total",
    ),
    "quantity": ("qty", "free_qty", "discount_pct", "gst_rate"),
    "text": ("name", "address", "product_name", "pack", "place_of_supply"),
}
# Leaf name -> class. A printed value whose leaf is not here (the serial number) is not scored.
FIELD_CLASSES: dict[str, FieldClass] = {
    leaf: field_class for field_class, leaves in _CLASS_MEMBERS.items() for leaf in leaves
}
# Unprinted on some invoices, and then the extraction must not supply a value.
NULL_WHEN_UNPRINTED = ("totals.cgst", "totals.sgst", "totals.igst")
_CITATION_TOLERANCE = 1.0  # points
# Values printed inside another value's box: "Gujarat (24)" carries the state and its code.
PRINTED_WITHIN = {"place_of_supply": ("place_of_supply_code",)}

_SEGMENT = re.compile(r"(\w+)|\[(\d+)\]")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FieldScore(_Model):
    path: str
    field_class: FieldClass
    expected: str | None
    actual: str | None
    outcome: Outcome
    cited: bool | None  # did a cited block cover the value's true position; None if no value


class DocumentScore(_Model):
    pair_id: str
    layout: Layout
    expected_lines: int
    extracted_lines: int
    error: str | None = None
    fields: tuple[FieldScore, ...]

    @property
    def scored(self) -> tuple[FieldScore, ...]:
        """Fields that count towards accuracy: the printed ones."""
        return tuple(f for f in self.fields if f.outcome in ("correct", "wrong", "missing"))

    @property
    def fully_correct(self) -> bool:
        return self.expected_lines == self.extracted_lines and all(
            field.outcome in ("correct", "correct_null") for field in self.fields
        )


class Tally(_Model):
    total: int
    correct: int
    wrong: int
    missing: int
    accuracy: float


class CitationTally(_Model):
    checked: int
    correct: int
    accuracy: float


class NullTally(_Model):
    total: int
    hallucinated: int


class EvalSummary(_Model):
    documents: int
    documents_fully_correct: int
    line_count_matches: int
    extra_lines: int  # line items extracted beyond those on the documents
    fields: Tally
    by_class: dict[str, Tally]
    by_layout: dict[str, Tally]
    citations: CitationTally
    null_expected: NullTally


def _leaf(path: str) -> str:
    return re.sub(r"\[\d+\]$", "", path.rsplit(".", 1)[-1])


def _resolve(root: object, path: str) -> object:
    """Follow a label path such as `lines[0].qty`. Raises LookupError if it does not exist."""
    node = root
    for name, index in _SEGMENT.findall(path):
        try:
            node = getattr(node, name) if name else node[int(index)]  # type: ignore[index]
        except (AttributeError, IndexError) as error:
            raise LookupError(path) from error
    return node


def _extracted(extraction: InvoiceExtraction | None, path: str) -> Extracted[object] | None:
    if extraction is None:
        return None
    try:
        field = _resolve(extraction, path)
    except LookupError:
        return None
    return field if isinstance(field, Extracted) else None


def _is_cited(field: Extracted[object], box: FieldBox, parsed: ParsedDocument | None) -> bool:
    if parsed is None:
        return False
    centre_x, centre_y = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    blocks = (parsed.block(block_id) for block_id in field.block_ids)
    return any(
        block is not None
        and block.page == box.page
        and block.bbox.contains(centre_x, centre_y, _CITATION_TOLERANCE)
        for block in blocks
    )


def score_invoice(
    label: PairLabel,
    extraction: InvoiceExtraction | None,
    parsed: ParsedDocument | None,
    error: str | None = None,
) -> DocumentScore:
    """`extraction=None` scores a document whose extraction failed: every field missing."""
    truth = label.documents["invoice"]
    fields: list[FieldScore] = []

    targets = [
        (path, box) for box in truth.boxes for path in (box.path, *PRINTED_WITHIN.get(box.path, ()))
    ]
    for path, box in targets:
        field_class = FIELD_CLASSES.get(_leaf(path))
        if field_class is None:
            continue
        expected = _resolve(label.invoice, path)
        field = _extracted(extraction, path)
        if field is None or field.value is None:
            outcome: Outcome = "missing"
            actual, cited = None, None
        else:
            outcome = "correct" if field.value == expected else "wrong"
            actual, cited = str(field.value), _is_cited(field, box, parsed)
        fields.append(
            FieldScore(
                path=path,
                field_class=field_class,
                expected=str(expected),
                actual=actual,
                outcome=outcome,
                cited=cited,
            )
        )

    for path in NULL_WHEN_UNPRINTED:
        # A failed extraction earns no credit for leaving these empty.
        if extraction is None or path not in truth.unprinted:
            continue
        field = _extracted(extraction, path)
        value = field.value if field is not None else None
        fields.append(
            FieldScore(
                path=path,
                field_class="amount",
                expected=None,
                actual=None if value is None else str(value),
                outcome="correct_null" if value is None else "hallucinated",
                cited=None,
            )
        )

    return DocumentScore(
        pair_id=label.pair_id,
        layout=label.layout,
        expected_lines=len(label.invoice.lines),
        extracted_lines=len(extraction.lines) if extraction is not None else 0,
        error=error,
        fields=tuple(fields),
    )


def _tally(fields: Iterable[FieldScore]) -> Tally:
    counts: dict[str, int] = defaultdict(int)
    for field in fields:
        counts[field.outcome] += 1
    total = counts["correct"] + counts["wrong"] + counts["missing"]
    return Tally(
        total=total,
        correct=counts["correct"],
        wrong=counts["wrong"],
        missing=counts["missing"],
        accuracy=round(counts["correct"] / total, 4) if total else 0.0,
    )


def summarize(scores: Iterable[DocumentScore]) -> EvalSummary:
    documents = list(scores)
    scored = [field for document in documents for field in document.scored]
    nulls = [
        field
        for document in documents
        for field in document.fields
        if field.outcome in ("correct_null", "hallucinated")
    ]
    cited = [field.cited for field in scored if field.cited is not None]
    return EvalSummary(
        documents=len(documents),
        documents_fully_correct=sum(document.fully_correct for document in documents),
        line_count_matches=sum(d.expected_lines == d.extracted_lines for d in documents),
        extra_lines=sum(max(0, d.extracted_lines - d.expected_lines) for d in documents),
        fields=_tally(scored),
        by_class={
            name: _tally(field for field in scored if field.field_class == name)
            for name in sorted({field.field_class for field in scored})
        },
        by_layout={
            layout: _tally(
                field
                for document in documents
                if document.layout == layout
                for field in document.scored
            )
            for layout in sorted({document.layout for document in documents})
        },
        citations=CitationTally(
            checked=len(cited),
            correct=sum(cited),
            accuracy=round(sum(cited) / len(cited), 4) if cited else 0.0,
        ),
        null_expected=NullTally(
            total=len(nulls),
            hallucinated=sum(field.outcome == "hallucinated" for field in nulls),
        ),
    )
