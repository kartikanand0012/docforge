"""Cutting a parsed document into chunks for search, each citing the blocks it came from.

Three kinds:
- `summary`: one per document, written from the extraction in plain words (type, numbers,
  parties, dates spelled out, products, batches). It is what answers "the invoice from X to Y
  in September", which no single printed block says. A general document's is its name and
  title line.
- `table_row`: one per table row, the cells joined with their column headers, so a line item
  is found and cited on its own.
- `text`: the other blocks of a page in reading order, grouped up to a size.
"""

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel

from docforge.parsing.base import Block, ParsedDocument
from docforge.trust.verify import extracted_fields

_TEXT_LIMIT = 800
_SUMMARY_CITATIONS = 12


@dataclass(frozen=True)
class Chunk:
    kind: str  # summary, table_row or text
    page: int
    block_ids: tuple[str, ...]
    text: str


def _day(value: date | None) -> str:
    return f"{value.day} {calendar.month_name[value.month]} {value.year}" if value else ""


def _month(value: str | None) -> str:
    """`2026-06` -> `June 2026`."""
    if not value:
        return ""
    year, month = value.split("-")
    return f"{calendar.month_name[int(month)]} {year}"


def _v(field: Any) -> Any:
    return getattr(field, "value", None)


def _say(template: str, value: Any) -> str:
    """`template` with the value filled in, or nothing when the value was not read."""
    return "" if value in (None, "") else template.format(value)


def _list(label: str, values: list[Any]) -> str:
    read = [str(value) for value in values if value not in (None, "")]
    return f" {label}: {', '.join(read)}." if read else ""


def _summary(doc_type: str, filename: str, extraction: BaseModel) -> str:
    """The document in words; a value that was not read is left out, not written as None."""
    e: Any = extraction
    if doc_type == "invoice":
        lines = list(e.lines)
        return (
            f"Invoice{_say(' {}', _v(e.invoice_no))}{_say(' dated {}', _day(_v(e.invoice_date)))}"
            f"{_say(' from {}', _v(e.seller.name))}{_say(' (GSTIN {})', _v(e.seller.gstin))}"
            f"{_say(' to {}', _v(e.buyer.name))}{_say(' (GSTIN {})', _v(e.buyer.gstin))}"
            f"{_say(', against purchase order {}', _v(e.po_no))}."
            f"{_list('Products', [_v(line.product_name) for line in lines])}"
            f"{_list('Batches', [_v(line.batch_no) for line in lines])}"
            f"{_say(' Grand total {}.', _v(e.totals.grand_total))}"
        )
    if doc_type == "purchase_order":
        return (
            f"Purchase order{_say(' {}', _v(e.po_no))}{_say(' dated {}', _day(_v(e.po_date)))}"
            f"{_say(' placed by {}', _v(e.buyer.name))}{_say(' with {}', _v(e.supplier_name))}."
            f"{_list('Products', [_v(line.product_name) for line in e.lines])}"
        )
    if doc_type == "coa":
        tests = "; ".join(
            f"{_v(t.name)} {_v(t.result)}{_say(' (limit {})', _v(t.specification))}"
            for t in e.tests
            if _v(t.name) is not None and _v(t.result) is not None
        )
        return (
            f"Certificate of analysis{_say(' {}', _v(e.coa_no))}"
            f"{_say(' from {}', _v(e.manufacturer))}{_say(' for {}', _v(e.product_name))}"
            f"{_say(' batch {}', _v(e.batch_no))}{_say(', manufactured {}', _month(_v(e.mfg)))}"
            f"{_say(', expiring {}', _month(_v(e.expiry)))}.{_say(' Tests: {}.', tests)}"
            f"{_say(' Conclusion: {}', _v(e.conclusion))}"
        )
    if doc_type == "general":
        return f"Document {filename}{_say(': {}', getattr(e, 'title', None))}."
    return f"{doc_type.replace('_', ' ').capitalize()} {filename}."


def chunk_document(
    doc_type: str, filename: str, parsed: ParsedDocument, extraction: BaseModel
) -> list[Chunk]:
    # The summary cites the document's own header fields (numbers, parties, dates, totals),
    # not every line: a hit on it should outline where the document says who and what.
    cited = tuple(
        dict.fromkeys(
            block_id
            for path, field in extracted_fields(extraction)
            if not path.startswith(("lines[", "tests[")) and "drug_licence" not in path
            for block_id in field.block_ids
        )
    )
    if doc_type == "general":  # nothing extracted: the summary cites the title line
        cited = tuple(b.id for b in parsed.blocks if b.kind == "text" and b.text.strip())[:1]
    known = {block.id: block for block in parsed.blocks}
    summary_blocks = tuple(b for b in cited if b in known)[:_SUMMARY_CITATIONS]
    first_page = known[summary_blocks[0]].page if summary_blocks else 1
    out = [Chunk("summary", first_page, summary_blocks, _summary(doc_type, filename, extraction))]

    headers: dict[tuple[int, int], str] = {}
    rows: dict[tuple[int, int], list[Block]] = {}
    for block in parsed.blocks:
        if block.kind == "table_cell" and block.table is not None and block.row is not None:
            if block.header:
                headers[(block.table, block.col or 0)] = block.text
            else:
                rows.setdefault((block.table, block.row), []).append(block)

    pending: list[Block] = []

    def flush() -> None:
        if pending:
            out.append(
                Chunk(
                    "text",
                    pending[0].page,
                    tuple(b.id for b in pending),
                    "\n".join(b.text for b in pending),
                )
            )
            pending.clear()

    emitted_rows: set[tuple[int, int]] = set()
    for block in parsed.blocks:
        if block.kind == "table_cell" and block.table is not None and block.row is not None:
            flush()
            if block.header:
                # A header row is a chunk of its own: the column names of the table.
                key = (block.table, -1 - (block.row or 0))
                if key not in emitted_rows:
                    emitted_rows.add(key)
                    cells = [
                        b
                        for b in parsed.blocks
                        if b.table == block.table and b.row == block.row and b.header
                    ]
                    out.append(
                        Chunk(
                            "table_row",
                            block.page,
                            tuple(c.id for c in cells),
                            " | ".join(c.text for c in cells),
                        )
                    )
                continue
            key = (block.table, block.row)
            if key in emitted_rows:
                continue
            emitted_rows.add(key)
            cells = rows[key]
            text = " | ".join(
                f"{headers[(c.table, c.col)]}: {c.text}" if (c.table, c.col) in headers else c.text
                for c in cells
                if c.table is not None and c.col is not None
            )
            out.append(Chunk("table_row", block.page, tuple(c.id for c in cells), text))
            continue
        if pending and (
            block.page != pending[0].page
            or sum(len(b.text) + 1 for b in pending) + len(block.text) > _TEXT_LIMIT
        ):
            flush()
        pending.append(block)
    flush()
    return out
