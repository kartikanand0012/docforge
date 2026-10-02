"""Docling-backed parser for born-digital PDFs (no OCR)."""

import io
import threading
from importlib.metadata import version
from typing import TYPE_CHECKING

from docforge.parsing.base import BBox, Block, Page, ParsedDocument, ParseError
from docforge.parsing.pdf import pdf_page_count

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc.base import BoundingBox

_MIN_SIDE = 0.01  # points; keeps a degenerate box valid instead of dropping its text


class DoclingParser:
    """Text items and table cells, in Docling's reading order, as blocks `b1`, `b2`, ...

    The models run on CPU: slower than a GPU but the same result on every machine, which
    the recorded evals depend on. Docling is imported on first use because it is slow to load.
    """

    name = "docling"

    def __init__(self, num_threads: int = 4) -> None:
        self.version = version("docling")
        self._num_threads = num_threads
        self._converter: DocumentConverter | None = None
        self._lock = threading.Lock()  # one conversion at a time; the models are not re-entrant

    def _get_converter(self) -> "DocumentConverter":
        if self._converter is None:
            from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption

            options = PdfPipelineOptions()
            options.do_ocr = False
            options.do_table_structure = True
            options.accelerator_options = AcceleratorOptions(
                device=AcceleratorDevice.CPU, num_threads=self._num_threads
            )
            self._converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
            )
        return self._converter

    def parse(self, pdf: bytes) -> ParsedDocument:
        from docling.datamodel.base_models import ConversionStatus, DocumentStream
        from docling_core.types.doc.items.table.table import TableItem
        from docling_core.types.doc.items.text import TextItem

        pdf_page_count(pdf)  # rejects unreadable files before the models are loaded
        stream = DocumentStream(name="document.pdf", stream=io.BytesIO(pdf))
        with self._lock:
            try:
                result = self._get_converter().convert(stream, raises_on_error=False)
            except Exception as error:
                raise ParseError("Docling failed to convert the document") from error
        if result.status != ConversionStatus.SUCCESS:
            raise ParseError(f"Docling conversion ended with status {result.status.value}")

        document = result.document
        heights = {number: page.size.height for number, page in document.pages.items()}
        blocks: list[Block] = []
        tables = 0

        def add(text: str, page: int, bbox: "BoundingBox", **cell: int | bool) -> None:
            if not text.strip():
                return
            blocks.append(
                Block(
                    id=f"b{len(blocks) + 1}",
                    kind="table_cell" if cell else "text",
                    text=text.strip(),
                    page=page,
                    bbox=_bottom_left(bbox, heights[page]),
                    **cell,
                )
            )

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
                        table=tables,
                        row=table_cell.start_row_offset_idx,
                        col=table_cell.start_col_offset_idx,
                        header=table_cell.column_header,
                    )
                tables += 1
            else:
                add(item.text, page, item.prov[0].bbox)

        return ParsedDocument(
            parser=self.name,
            parser_version=self.version,
            pages=tuple(
                Page(number=number, width=page.size.width, height=page.size.height)
                for number, page in sorted(document.pages.items())
            ),
            blocks=tuple(blocks),
        )


def _bottom_left(bbox: "BoundingBox", page_height: float) -> BBox:
    """Docling mixes top-left and bottom-left origins; blocks always use bottom-left."""
    box = bbox.to_bottom_left_origin(page_height)
    x0, x1 = sorted((box.l, box.r))
    y0, y1 = sorted((box.b, box.t))
    return BBox(
        x0=round(x0, 2),
        y0=round(y0, 2),
        x1=round(max(x1, x0 + _MIN_SIDE), 2),
        y1=round(max(y1, y0 + _MIN_SIDE), 2),
    )
