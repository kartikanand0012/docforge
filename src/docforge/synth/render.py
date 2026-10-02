"""Render the synthetic documents as born-digital PDFs and record where each value is printed."""

import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen.canvas import Canvas

from docforge.synth.models import (
    FieldBox,
    Invoice,
    InvoiceLine,
    Layout,
    Party,
    PurchaseOrder,
    PurchaseOrderLine,
    leaf_paths,
)

_REGULAR = "Helvetica"
_BOLD = "Helvetica-Bold"
_MARGIN = 30.0
_CELL_PAD = 2.0

Align = Literal["left", "right"]
DateStyle = Literal["dd-Mon-yyyy", "dd/mm/yyyy"]
# Not strftime("%b"): that follows the process locale, and the output must not.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class RenderedDocument:
    pdf: bytes
    page_width: float
    page_height: float
    boxes: tuple[FieldBox, ...]
    unprinted: tuple[str, ...]  # label paths that appear nowhere on the page


@dataclass(frozen=True)
class _Column[LineT]:
    field: str
    header: str
    weight: float
    align: Align
    text: Callable[[LineT], str | None]


@dataclass(frozen=True)
class _Style:
    page: tuple[float, float]
    font_size: float
    row_pitch: float
    dates: DateStyle


_INVOICE_STYLES: dict[Layout, _Style] = {
    "A": _Style(page=landscape(A4), font_size=7.5, row_pitch=14.0, dates="dd-Mon-yyyy"),
    "B": _Style(page=A4, font_size=6.5, row_pitch=13.0, dates="dd/mm/yyyy"),
}
_ORDER_STYLES: dict[Layout, _Style] = {
    "A": _Style(page=A4, font_size=8.5, row_pitch=15.0, dates="dd-Mon-yyyy"),
    "B": _Style(page=A4, font_size=8.0, row_pitch=14.0, dates="dd/mm/yyyy"),
}


class _Page:
    """A single PDF page that remembers the box of every ground-truth value it draws."""

    def __init__(self, style: _Style) -> None:
        self.style = style
        self.width, self.height = style.page
        self._buffer = io.BytesIO()
        # invariant fixes the timestamps and document id; leaving streams uncompressed keeps
        # the bytes independent of the zlib build. Together they make output reproducible.
        self._canvas = Canvas(self._buffer, pagesize=style.page, invariant=1, pageCompression=0)
        self._boxes: list[FieldBox] = []

    def text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        size: float | None = None,
        bold: bool = False,
        align: Align = "left",
        path: str | None = None,
    ) -> float:
        """Draw `text` at baseline `y`; return the x where it ends. `path` records a box."""
        font = _BOLD if bold else _REGULAR
        size = size or self.style.font_size
        width = float(pdfmetrics.stringWidth(text, font, size))
        left = x - width if align == "right" else x
        self._canvas.setFont(font, size)
        self._canvas.drawString(left, y, text)
        if path is not None:
            ascent, descent = pdfmetrics.getAscentDescent(font, size)
            self._boxes.append(
                FieldBox(
                    path=path,
                    text=text,
                    page=1,
                    x0=round(left, 2),
                    y0=round(y + descent, 2),
                    x1=round(left + width, 2),
                    y1=round(y + ascent, 2),
                )
            )
        return left + width

    def labelled(self, x: float, y: float, label: str, value: str, path: str) -> float:
        """Draw `label` then `value`; only the value is boxed."""
        end = self.text(x, y, label)
        return self.text(end + 3, y, value, path=path)

    def rule(self, y: float) -> None:
        self._canvas.setLineWidth(0.5)
        self._canvas.line(_MARGIN, y, self.width - _MARGIN, y)

    def table[LineT](
        self, top: float, columns: tuple[_Column[LineT], ...], rows: tuple[LineT, ...]
    ) -> float:
        """Draw a header at baseline `top` and one row per line; return the y below the table."""
        span = self.width - 2 * _MARGIN
        total_weight = sum(column.weight for column in columns)
        self.rule(top + self.style.row_pitch * 0.75)
        left = _MARGIN
        anchors: list[float] = []
        for column in columns:
            width = span * column.weight / total_weight
            anchor = left + width - _CELL_PAD if column.align == "right" else left + _CELL_PAD
            anchors.append(anchor)
            self.text(anchor, top, column.header, bold=True, align=column.align)
            left += width
        self.rule(top - self.style.row_pitch * 0.35)

        y = top
        for row_index, row in enumerate(rows):
            y -= self.style.row_pitch
            for column, anchor in zip(columns, anchors, strict=True):
                value = column.text(row)
                if value is None:
                    continue
                path = f"lines[{row_index}].{column.field}"
                self.text(anchor, y, value, align=column.align, path=path)
        bottom = y - self.style.row_pitch * 0.35
        self.rule(bottom)
        return bottom

    def finish(self, truth: BaseModel) -> RenderedDocument:
        """Close the page. `truth` is the label the page was drawn from."""
        self._canvas.showPage()
        self._canvas.save()
        return RenderedDocument(
            pdf=self._buffer.getvalue(),
            page_width=float(self.width),
            page_height=float(self.height),
            boxes=tuple(self._boxes),
            unprinted=tuple(sorted(leaf_paths(truth) - {box.path for box in self._boxes})),
        )


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _plain(value: Decimal) -> str:
    """A rate or percentage without trailing zeros: 12, 2.5, 0."""
    return format(value.normalize(), "f")


def _month(value: str) -> str:
    """Month `2028-03` as printed on a pack: `03/28`."""
    return f"{value[5:7]}/{value[2:4]}"


def _party_block(page: _Page, x: float, y: float, party: Party, prefix: str, pitch: float) -> float:
    """Name, address, GSTIN and licence numbers; return the y below the block."""
    page.text(x, y, party.name, bold=True, size=page.style.font_size + 2.5, path=f"{prefix}.name")
    y -= pitch
    page.text(x, y, party.address, path=f"{prefix}.address")
    y -= pitch
    page.labelled(x, y, "GSTIN:", party.gstin, f"{prefix}.gstin")
    y -= pitch
    end = page.text(x, y, "D.L. No.:")
    last = len(party.drug_licence_nos) - 1
    for index, licence in enumerate(party.drug_licence_nos):
        end = page.text(end + 3, y, licence, path=f"{prefix}.drug_licence_nos[{index}]")
        if index < last:
            end = page.text(end, y, ",")
    return y - pitch


_INVOICE_COLUMNS: dict[Layout, tuple[_Column[InvoiceLine], ...]] = {
    "A": (
        _Column("sl_no", "Sl", 16, "right", lambda line: str(line.sl_no)),
        _Column("product_name", "Product", 130, "left", lambda line: line.product_name),
        _Column("pack", "Pack", 34, "left", lambda line: line.pack),
        _Column("hsn", "HSN", 42, "left", lambda line: line.hsn),
        _Column("batch_no", "Batch", 52, "left", lambda line: line.batch_no),
        _Column("mfg", "Mfg", 30, "left", lambda line: _month(line.mfg)),
        _Column("expiry", "Exp", 30, "left", lambda line: _month(line.expiry)),
        _Column("qty", "Qty", 28, "right", lambda line: str(line.qty)),
        _Column("free_qty", "Free", 26, "right", lambda line: str(line.free_qty)),
        _Column("mrp", "MRP", 44, "right", lambda line: _money(line.mrp)),
        _Column("ptr", "PTR", 44, "right", lambda line: _money(line.ptr)),
        _Column("discount_pct", "Disc%", 30, "right", lambda line: _plain(line.discount_pct)),
        _Column("taxable_value", "Taxable", 56, "right", lambda line: _money(line.taxable_value)),
        _Column("gst_rate", "GST%", 28, "right", lambda line: _plain(line.gst_rate)),
        _Column("amount", "Amount", 58, "right", lambda line: _money(line.amount)),
    ),
    "B": (
        _Column("sl_no", "#", 14, "right", lambda line: str(line.sl_no)),
        _Column("hsn", "HSN/SAC", 38, "left", lambda line: line.hsn),
        _Column(
            "product_name", "Description of Goods", 112, "left", lambda line: line.product_name
        ),
        _Column("batch_no", "Batch No.", 46, "left", lambda line: line.batch_no),
        _Column("expiry", "Expiry", 26, "left", lambda line: _month(line.expiry)),
        _Column("mfg", "Mfg.", 26, "left", lambda line: _month(line.mfg)),
        _Column("pack", "Packing", 32, "left", lambda line: line.pack),
        _Column("mrp", "M.R.P.", 36, "right", lambda line: _money(line.mrp)),
        _Column("qty", "Qty", 22, "right", lambda line: str(line.qty)),
        _Column("free_qty", "Sch", 20, "right", lambda line: str(line.free_qty)),
        _Column("ptr", "Rate", 36, "right", lambda line: _money(line.ptr)),
        _Column("discount_pct", "D%", 20, "right", lambda line: _plain(line.discount_pct)),
        _Column("gst_rate", "Tax%", 22, "right", lambda line: _plain(line.gst_rate)),
        _Column(
            "taxable_value", "Taxable Amt", 48, "right", lambda line: _money(line.taxable_value)
        ),
        _Column("amount", "Net Amt", 50, "right", lambda line: _money(line.amount)),
    ),
}

_ORDER_COLUMNS: dict[Layout, tuple[_Column[PurchaseOrderLine], ...]] = {
    "A": (
        _Column("sl_no", "Sl", 20, "right", lambda line: str(line.sl_no)),
        _Column("product_name", "Item", 220, "left", lambda line: line.product_name),
        _Column("pack", "Pack", 60, "left", lambda line: line.pack),
        _Column("qty", "Qty", 50, "right", lambda line: str(line.qty)),
        _Column("scheme", "Scheme", 60, "right", lambda line: line.scheme),
        _Column("rate", "Rate (PTR)", 80, "right", lambda line: _money(line.rate)),
    ),
    "B": (
        _Column("sl_no", "No.", 24, "right", lambda line: str(line.sl_no)),
        _Column("product_name", "Product Description", 210, "left", lambda line: line.product_name),
        _Column("qty", "Order Qty", 60, "right", lambda line: str(line.qty)),
        _Column("scheme", "Free Scheme", 70, "right", lambda line: line.scheme),
        _Column("pack", "Packing", 60, "left", lambda line: line.pack),
        _Column("rate", "Unit Rate", 80, "right", lambda line: _money(line.rate)),
    ),
}


def _invoice_meta(invoice: Invoice, layout: Layout, style: _Style) -> list[tuple[str, str, str]]:
    """(label, value, path) for the invoice header; the two layouts word the labels differently."""
    place = f"{invoice.place_of_supply} ({invoice.place_of_supply_code})"
    invoice_date = _dated(invoice.invoice_date, style)
    po_date = _dated(invoice.po_date, style)
    if layout == "A":
        return [
            ("Invoice No.:", invoice.invoice_no, "invoice_no"),
            ("Date:", invoice_date, "invoice_date"),
            ("PO No.:", invoice.po_no, "po_no"),
            ("PO Date:", po_date, "po_date"),
            ("Place of Supply:", place, "place_of_supply"),
        ]
    return [
        ("Inv. Number", invoice.invoice_no, "invoice_no"),
        ("Inv. Date", invoice_date, "invoice_date"),
        ("Order Ref.", invoice.po_no, "po_no"),
        ("Order Date", po_date, "po_date"),
        ("Place of Supply:", place, "place_of_supply"),
    ]


def _dated(value: date, style: _Style) -> str:
    if style.dates == "dd-Mon-yyyy":
        return f"{value.day:02d}-{_MONTHS[value.month - 1]}-{value.year}"
    return f"{value.day:02d}/{value.month:02d}/{value.year}"


def _totals_rows(invoice: Invoice) -> list[tuple[str, str, str]]:
    """(label, value, path) for the totals block; tax rows depend on the supply type."""
    totals = invoice.totals
    rows = [("Taxable Value", _money(totals.taxable_value), "totals.taxable_value")]
    if invoice.supply_type == "intra_state":
        rows.append(("CGST", _money(totals.cgst), "totals.cgst"))
        rows.append(("SGST", _money(totals.sgst), "totals.sgst"))
    else:
        rows.append(("IGST", _money(totals.igst), "totals.igst"))
    rows.append(("Round Off", _money(totals.round_off), "totals.round_off"))
    rows.append(("Grand Total", _money(totals.grand_total), "totals.grand_total"))
    return rows


def render_invoice(invoice: Invoice, layout: Layout) -> RenderedDocument:
    """Layout A is landscape with the seller top-left; B is portrait with the seller top-right."""
    style = _INVOICE_STYLES[layout]
    page = _Page(style)
    pitch = style.font_size + 4.5
    top = page.height - _MARGIN - 10
    meta = _invoice_meta(invoice, layout, style)

    if layout == "A":
        page.text(page.width / 2 - 30, top + 8, "TAX INVOICE", bold=True, size=11)
        seller_bottom = _party_block(page, _MARGIN, top - 12, invoice.seller, "seller", pitch)
        y = top - 12
        for label, value, path in meta:
            page.labelled(page.width - 250, y, label, value, path)
            y -= pitch
        page.text(_MARGIN, seller_bottom - 6, "Bill To:", bold=True)
        buyer_top = seller_bottom - 6 - pitch
    else:
        page.text(_MARGIN, top + 6, "GST INVOICE", bold=True, size=12)
        _party_block(page, page.width / 2 + 10, top + 6, invoice.seller, "seller", pitch)
        y = top - 14
        for label, value, path in meta:
            page.labelled(_MARGIN, y, label, value, path)
            y -= pitch
        page.text(_MARGIN, y - 8, "Buyer (Bill to)", bold=True)
        buyer_top = y - 8 - pitch
    buyer_bottom = _party_block(page, _MARGIN, buyer_top, invoice.buyer, "buyer", pitch)

    table_bottom = page.table(
        buyer_bottom - style.row_pitch, _INVOICE_COLUMNS[layout], invoice.lines
    )

    y = table_bottom - pitch - 4
    label_x = page.width - 200 if layout == "A" else _MARGIN
    value_x = page.width - _MARGIN - _CELL_PAD if layout == "A" else _MARGIN + 170
    for label, value, path in _totals_rows(invoice):
        bold = path == "totals.grand_total"
        page.text(label_x, y, label, bold=bold)
        page.text(value_x, y, value, bold=bold, align="right", path=path)
        y -= pitch
    return page.finish(invoice)


def render_purchase_order(order: PurchaseOrder, layout: Layout) -> RenderedDocument:
    """The buyer's order. Layout A puts the order details on the right; B puts them first."""
    style = _ORDER_STYLES[layout]
    page = _Page(style)
    pitch = style.font_size + 4.5
    top = page.height - _MARGIN - 10
    po_date = _dated(order.po_date, style)

    page.text(_MARGIN, top + 6, "PURCHASE ORDER", bold=True, size=12)
    if layout == "A":
        buyer_bottom = _party_block(page, _MARGIN, top - 16, order.buyer, "buyer", pitch)
        page.labelled(page.width - 220, top - 16, "PO No.:", order.po_no, "po_no")
        page.labelled(page.width - 220, top - 16 - pitch, "PO Date:", po_date, "po_date")
    else:
        page.labelled(_MARGIN, top - 16, "Order Number", order.po_no, "po_no")
        page.labelled(page.width / 2, top - 16, "Dated", po_date, "po_date")
        buyer_bottom = _party_block(
            page, _MARGIN, top - 16 - 2 * pitch, order.buyer, "buyer", pitch
        )

    y = buyer_bottom - 8
    page.text(_MARGIN, y, "To:" if layout == "A" else "Supplier", bold=True)
    page.text(_MARGIN + 50, y, order.supplier_name, bold=True, path="supplier_name")
    page.labelled(_MARGIN + 50, y - pitch, "GSTIN:", order.supplier_gstin, "supplier_gstin")

    page.table(y - 2 * pitch - style.row_pitch, _ORDER_COLUMNS[layout], order.lines)
    return page.finish(order)
