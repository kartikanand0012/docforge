"""Docling-backed parser: the text layer of born-digital PDFs, OCR for scans.

A scan is rendered, straightened and read by OCR; its blocks are then mapped back so their
boxes sit on the page as it was uploaded. Long documents are converted a few pages at a time,
so memory is bounded by the batch and not by the length of the file.
"""

import io
import threading
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import TYPE_CHECKING

from docforge.parsing.base import BBox, Block, NoTextLayer, Page, ParsedDocument, ParseError
from docforge.parsing.pdf import has_text_layer, pdf_page_count
from docforge.parsing.raster import estimate_skew, images_to_pdf, render_pages, rotate_box

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc.base import BoundingBox
    from docling_core.types.doc.document import DoclingDocument

_MIN_SIDE = 0.01  # points; keeps a degenerate box valid instead of dropping its text
_OCR_DPI = 200  # scans are re-rendered at this resolution before OCR, whatever their own
_OCR_QUALITY = 90
_MIN_SKEW = 0.2  # degrees; below this a page is left as it is
_DEFAULT_BATCH_PAGES = 10


@dataclass
class _Parts:
    """Blocks, pages and the table count gathered so far, across batches."""

    blocks: list[Block] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)
    tables: int = 0


class DoclingParser:
    """Text items and table cells, in Docling's reading order, as blocks `b1`, `b2`, ...

    The models run on CPU: slower than a GPU but the same result on every machine, which
    the recorded evals depend on. Docling is imported on first use because it is slow to load.
    """

    name = "docling"

    def __init__(
        self, num_threads: int = 4, *, ocr: bool = True, batch_pages: int = _DEFAULT_BATCH_PAGES
    ) -> None:
        if batch_pages < 1:
            raise ValueError("batch_pages must be at least 1")
        self.version = version("docling")
        self._num_threads = num_threads
        self._ocr = ocr
        self._batch_pages = batch_pages
        self._converters: dict[bool, DocumentConverter] = {}
        self._lock = threading.Lock()  # one conversion at a time; the models are not re-entrant

    def _get_converter(self, ocr: bool) -> "DocumentConverter":
        if ocr not in self._converters:
            from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                OcrMode,
                PdfPipelineOptions,
                RapidOcrOptions,
            )
            from docling.document_converter import DocumentConverter, PdfFormatOption

            options = PdfPipelineOptions()
            options.do_ocr = ocr
            if ocr:
                # The whole page is an image, so the whole page is read. The torch backend
                # needs no dependency beyond the one the layout models already use.
                options.ocr_options = RapidOcrOptions(mode=OcrMode.FULL_PAGE, backend="torch")
            options.do_table_structure = True
            options.accelerator_options = AcceleratorOptions(
                device=AcceleratorDevice.CPU, num_threads=self._num_threads
            )
            self._converters[ocr] = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
            )
        return self._converters[ocr]

    def parse(self, pdf: bytes) -> ParsedDocument:
        page_count = pdf_page_count(pdf)  # rejects unreadable files before the models are loaded
        scanned = not has_text_layer(pdf)
        if scanned and not self._ocr:
            raise NoTextLayer("the PDF has no text layer and OCR is switched off")

        parts = _Parts()
        for first in range(1, page_count + 1, self._batch_pages):
            last = min(first + self._batch_pages - 1, page_count)
            with self._lock:
                if scanned:
                    self._read_scan(pdf, first, last, parts)
                else:
                    whole = (first, last) == (1, page_count)
                    document = self._convert(pdf, False, None if whole else (first, last))
                    self._add(document, parts, offset=0, angles={})
        try:
            return ParsedDocument(
                parser=self.name,
                parser_version=self.version,
                source="ocr" if scanned else "text_layer",
                pages=tuple(parts.pages),
                blocks=tuple(parts.blocks),
            )
        except ValueError as error:  # includes pydantic validation errors
            raise ParseError("Docling output could not be converted to blocks") from error

    def _read_scan(self, pdf: bytes, first: int, last: int, parts: _Parts) -> None:
        """Render pages `first`..`last`, straighten them, read them, and map the boxes back."""
        images = render_pages(pdf, _OCR_DPI, first, last)
        angles: dict[int, float] = {}
        for index, image in enumerate(images):
            angle = estimate_skew(image)
            if abs(angle) >= _MIN_SKEW:
                from PIL import Image

                images[index] = image.rotate(
                    angle, resample=Image.Resampling.BICUBIC, fillcolor=255
                )
                angles[index + 1] = angle
        straightened = images_to_pdf(images, _OCR_DPI, _OCR_QUALITY)
        self._add(self._convert(straightened, True, None), parts, offset=first - 1, angles=angles)

    def _convert(
        self, pdf: bytes, ocr: bool, page_range: tuple[int, int] | None
    ) -> "DoclingDocument":
        from docling.datamodel.base_models import ConversionStatus, DocumentStream

        stream = DocumentStream(name="document.pdf", stream=io.BytesIO(pdf))
        try:
            if page_range is None:
                result = self._get_converter(ocr).convert(stream, raises_on_error=False)
            else:
                result = self._get_converter(ocr).convert(
                    stream, raises_on_error=False, page_range=page_range
                )
        except Exception as error:
            raise ParseError("Docling failed to convert the document") from error
        if result.status != ConversionStatus.SUCCESS:
            raise ParseError(f"Docling conversion ended with status {result.status.value}")
        return result.document

    def _add(
        self, document: "DoclingDocument", parts: _Parts, offset: int, angles: dict[int, float]
    ) -> None:
        """Append one converted batch. `offset` is added to its page numbers; `angles` are
        the rotations that were applied to its pages, undone here for every box.
        """
        from docling_core.types.doc.items.table.table import TableItem
        from docling_core.types.doc.items.text import TextItem

        sizes = {number: page.size for number, page in document.pages.items()}

        def add(text: str, page: int, bbox: "BoundingBox", **cell: int | bool) -> None:
            if not text.strip():
                return
            size = sizes[page]
            try:
                box = _bottom_left(bbox, size.height)
                if page in angles:
                    box = _box(
                        *rotate_box(
                            (box.x0, box.y0, box.x1, box.y1), -angles[page], size.width, size.height
                        )
                    )
                parts.blocks.append(
                    Block(
                        id=f"b{len(parts.blocks) + 1}",
                        kind="table_cell" if cell else "text",
                        text=text.strip(),
                        page=page + offset,
                        bbox=box,
                        **cell,
                    )
                )
            except (ValueError, KeyError) as error:
                raise ParseError("Docling output could not be converted to blocks") from error

        for item, _level in document.iterate_items():
            if not isinstance(item, TableItem | TextItem) or not item.prov:
                continue
            page = item.prov[0].page_no
            if isinstance(item, TableItem):
                for table_cell in item.data.table_cells:
                    if table_cell.bbox is None:
                        continue
                    add(
                        table_cell.text,
                        page,
                        table_cell.bbox,
                        table=parts.tables,
                        row=table_cell.start_row_offset_idx,
                        col=table_cell.start_col_offset_idx,
                        header=table_cell.column_header,
                    )
                parts.tables += 1
            else:
                add(item.text, page, item.prov[0].bbox)

        parts.pages.extend(
            Page(number=number + offset, width=size.width, height=size.height)
            for number, size in sorted(sizes.items())
        )


def _bottom_left(bbox: "BoundingBox", page_height: float) -> BBox:
    """Docling mixes top-left and bottom-left origins; blocks always use bottom-left."""
    box = bbox.to_bottom_left_origin(page_height)
    x0, x1 = sorted((box.l, box.r))
    y0, y1 = sorted((box.b, box.t))
    return _box(x0, y0, x1, y1)


def _box(x0: float, y0: float, x1: float, y1: float) -> BBox:
    return BBox(
        x0=round(x0, 2),
        y0=round(y0, 2),
        x1=round(max(x1, x0 + _MIN_SIDE), 2),
        y1=round(max(y1, y0 + _MIN_SIDE), 2),
    )
