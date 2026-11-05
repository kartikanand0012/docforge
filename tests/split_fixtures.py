"""Files holding several invoices, with known ground truth, and a parser that reads them.

`TextParser` reads the real text of a PDF with its positions (pypdf), as a parser would: so
the splitter is tested on what the synthetic invoices actually print, labels included.
`BundleParser` also knows each fixture invoice: a PDF holding exactly one of them is parsed
as a perfect parser would (`cited`), so the invoices cut from a bundle are read correctly.
"""

import io
import json
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from docforge.llm.base import LLMRequest, LLMResponse
from docforge.parsing.base import BBox, Block, Page, ParsedDocument
from fakes import cited

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def text_parse(pdf: bytes) -> ParsedDocument:
    reader = PdfReader(io.BytesIO(pdf))
    pages: list[Page] = []
    blocks: list[Block] = []
    for number, page in enumerate(reader.pages, start=1):
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        pages.append(Page(number=number, width=width, height=height))
        runs: list[tuple[str, float, float, float]] = []

        def visit(text: str, cm: Any, tm: Any, font: Any, size: float, runs: Any = runs) -> None:
            if text.strip():
                runs.append((text.strip(), float(tm[4]), float(tm[5]), float(size or 8)))

        page.extract_text(visitor_text=visit)
        for index, (text, x, y, size) in enumerate(runs):
            x0, y0 = max(x, 0.0), max(y - 1, 0.0)
            blocks.append(
                Block(
                    id=f"p{number}b{index}", kind="text", text=text, page=number,
                    bbox=BBox(x0=x0, y0=y0, x1=x0 + max(len(text) * size * 0.5, 1.0),
                              y1=y0 + size + 1),
                )
            )  # fmt: skip
    return ParsedDocument(
        parser="text", parser_version="0", pages=tuple(pages), blocks=tuple(blocks)
    )


class TextParser:
    name = "text"
    version = "0"

    def __init__(self) -> None:
        self.calls = 0

    def parse(self, pdf: bytes) -> ParsedDocument:
        self.calls += 1
        return text_parse(pdf)


def bundle(*pdfs: bytes, pages: list[tuple[int, int]] | None = None) -> bytes:
    """The PDFs one after the other; with `pages`, only those (pdf index, page) in order."""
    readers = [PdfReader(io.BytesIO(pdf)) for pdf in pdfs]
    writer = PdfWriter()
    chosen = pages or [(i, n) for i, r in enumerate(readers) for n in range(1, len(r.pages) + 1)]
    for index, number in chosen:
        writer.add_page(readers[index].pages[number - 1])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def letter() -> bytes:
    """A covering letter: no invoice number, no GSTIN."""
    out = io.BytesIO()
    canvas = Canvas(out, pagesize=A4, invariant=1)
    for line, text in enumerate([
        "To the Accounts Department",
        "Please find enclosed our bills for October 2026.",
        "Kindly arrange payment at the earliest.",
        "Regards, Accounts",
    ]):  # fmt: skip
        canvas.drawString(72, 760 - line * 18, text)
    canvas.showPage()
    canvas.save()
    return out.getvalue()


class Fixture:
    """One fixture invoice: its PDF, label, the raw a perfect model replies, and its parse."""

    def __init__(self, directory: Path, raw_from_label: Any) -> None:
        self.label = json.loads((directory / "label.json").read_text(encoding="utf-8"))
        self.pdf = (directory / "invoice.pdf").read_bytes()
        self.raw = raw_from_label(self.label)
        self.parsed = cited(self.label, "invoice", self.raw)
        self.invoice_no: str = self.label["invoice"]["invoice_no"]
        self.seller: str = self.label["invoice"]["seller"]["name"]
        self.total: str = self.label["invoice"]["totals"]["grand_total"]
        self.pages = len(PdfReader(io.BytesIO(self.pdf)).pages)


def fixture(name: str, raw_from_label: Any) -> Fixture:
    kind = "multipage" if int(name.rsplit("_", 1)[1]) > 100 else "synthetic"
    return Fixture(FIXTURES / kind / name, raw_from_label)


class BundleParser(TextParser):
    """Reads the real text of any PDF; a PDF holding just one known invoice is parsed as a
    perfect parser would, so its extraction is exact."""

    def __init__(self, known: list[Fixture]) -> None:
        super().__init__()
        self._known = known

    def parse(self, pdf: bytes) -> ParsedDocument:
        parsed = super().parse(pdf)
        text = " ".join(block.text for block in parsed.blocks)
        found = [f for f in self._known if f.invoice_no in text]
        if len(found) == 1 and found[0].pages == len(parsed.pages):
            return found[0].parsed
        return parsed


class ByNumberProvider:
    """Replies with the raw of the invoice whose number the prompt holds (else the last)."""

    name = "fake"
    model = "fake-1"

    def __init__(self, known: list[Fixture]) -> None:
        self._known = known
        self._last: Fixture | None = None
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        found = [f for f in self._known if f.invoice_no in request.prompt]
        chosen = found[0] if found else self._last
        assert chosen is not None, "the prompt holds no known invoice"
        self._last = chosen
        return LLMResponse(
            text=json.dumps(chosen.raw), provider=self.name, model=self.model,
            input_tokens=100, output_tokens=50, latency_ms=1.0,
        )  # fmt: skip
