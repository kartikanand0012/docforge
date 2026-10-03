"""Cutting a parsed document into chunks for search, each citing the blocks it came from.

Three kinds:
- `summary`: one per document, written from the extraction in plain words (type, numbers,
  parties, dates spelled out, products, batches). It is what answers "the invoice from X to Y
  in September", which no single printed block says.
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


def _summary(doc_type: str, filename: str, extraction: BaseModel) -> str:
    e: Any = extraction
    if doc_type == "invoice":
        lines = list(e.lines)
        return (
            f"Invoice {_v(e.invoice_no)} dated {_day(_v(e.invoice_date))} from "
            f"{_v(e.seller.name)} (GSTIN {_v(e.seller.gstin)}) to {_v(e.buyer.name)} "
            f"(GSTIN {_v(e.buyer.gstin)}), against purchase order {_v(e.po_no)}. "
            f"Products: {', '.join(str(_v(line.product_name)) for line in lines)}. "
            f"Batches: {', '.join(str(_v(line.batch_no)) for line in lines)}. "
            f"Grand total {_v(e.totals.grand_total)}."
        )
    if doc_type == "purchase_order":
        return (
            f"Purchase order {_v(e.po_no)} dated {_day(_v(e.po_date))} placed by "
            f"{_v(e.buyer.name)} with {_v(e.supplier_name)}. "
            f"Products: {', '.join(str(_v(line.product_name)) for line in e.lines)}."
        )
    if doc_type == "coa":
        tests = "; ".join(
            f"{_v(t.name)} {_v(t.result)} (limit {_v(t.specification)})" for t in e.tests
        )
        return (
            f"Certificate of analysis {_v(e.coa_no)} from {_v(e.manufacturer)} for "
            f"{_v(e.product_name)} batch {_v(e.batch_no)}, manufactured {_month(_v(e.mfg))}, "
            f"expiring {_month(_v(e.expiry))}. Tests: {tests}. Conclusion: {_v(e.conclusion)}"
        )
    return f"{doc_type.replace('_', ' ').capitalize()} {filename}."


def chunk_document(
    doc_type: str, filename: str, parsed: ParsedDocument, extraction: BaseModel
) -> list[Chunk]:
    cited = tuple(
        dict.fromkeys(
            block_id for _, field in extracted_fields(extraction) for block_id in field.block_ids
        )
    )
    known = {block.id: block for block in parsed.blocks}
    summary_blocks = tuple(b for b in cited if b in known)[:50]
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
