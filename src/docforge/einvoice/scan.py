"""Finding the QR codes printed on an invoice's pages.

A signed e-invoice QR is usually on the first page, sometimes on the last. Pages are read in
that order, then pages two to five, at 300 dpi; the first page again at 450 dpi when nothing
signed-looking was found, for QRs printed small. The search stops at the first signed-looking
QR. zxing-cpp decodes the dense ~900-character QR that OpenCV's detector misses.

The decoder is native code reading pixels from files we did not write, so it runs in its own
child process with time and memory limits (`isolated_scanner`), as the parser does - but
apart from it, so that cached and replayed parses are not changed by it.
"""

from importlib.metadata import version

import zxingcpp
from pydantic import BaseModel, ConfigDict

from docforge.parsing.isolation import IsolatedParser
from docforge.parsing.pdf import pdf_page_count
from docforge.parsing.raster import render_pages

MAX_PAGES = 5  # beyond the last page, read first
MAX_CODES_PER_PAGE = 8
MAX_TEXT_CHARS = 4096
DPI, SMALL_QR_DPI = 300, 450
_MIN_SIGNED_CHARS = 200


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FoundQr(_Model):
    page: int
    text: str


class QrScan(_Model):
    found: tuple[FoundQr, ...]
    pages_scanned: int
    decoder: str


def looks_signed(text: str) -> bool:
    """Shaped like a compact JWS: three dotted parts and long enough to hold a signature."""
    parts = text.split(".")
    return len(parts) == 3 and all(parts) and len(text) >= _MIN_SIGNED_CHARS and " " not in text


def _order(pages: int) -> list[int]:
    wanted = [1, pages, *range(2, min(pages, MAX_PAGES) + 1)]
    return [page for index, page in enumerate(wanted) if page >= 1 and page not in wanted[:index]]


def _read(pdf: bytes, page: int, dpi: int) -> list[str]:
    (image,) = render_pages(pdf, dpi, page, page)
    codes = zxingcpp.read_barcodes(
        image, formats=zxingcpp.BarcodeFormats(zxingcpp.BarcodeFormat.QRCode)
    )
    return [code.text for code in codes[:MAX_CODES_PER_PAGE] if code.valid and code.text]


class QrScanner:
    """Reads a PDF's QR codes. Named like a parser so that `IsolatedParser` can run it."""

    name = "zxing-cpp"
    version = version("zxing-cpp")

    def parse(self, pdf: bytes) -> QrScan:
        """Raises `ParseError` (e.g. `DocumentTooLarge`) when a page cannot be rendered."""
        found: list[FoundQr] = []
        scanned = 0
        for page in _order(pdf_page_count(pdf)):
            scanned += 1
            texts = _read(pdf, page, DPI)
            if page == 1 and not any(looks_signed(text) for text in texts):
                texts += [text for text in _read(pdf, 1, SMALL_QR_DPI) if text not in texts]
            found += [
                FoundQr(page=page, text=text)
                for text in texts
                if len(text) <= MAX_TEXT_CHARS and all(text != seen.text for seen in found)
            ]
            if any(looks_signed(seen.text) for seen in found):
                break
        decoder = f"zxing-cpp {self.version}"
        return QrScan(found=tuple(found), pages_scanned=scanned, decoder=decoder)


class IsolatedScanner(IsolatedParser):
    """`QrScanner` in a child process with time and memory limits."""

    def parse(self, pdf: bytes) -> QrScan:  # type: ignore[override]
        return QrScan.model_validate_json(self._run(pdf))


def isolated_scanner(
    *, timeout_seconds: float, max_rss_mb: int, max_documents: int = 200
) -> IsolatedScanner:
    return IsolatedScanner(
        QrScanner,  # type: ignore[arg-type]
        name=QrScanner.name,
        version=QrScanner.version,
        max_documents=max_documents,
        timeout_seconds=timeout_seconds,
        max_rss_bytes=max_rss_mb * 1024 * 1024,
    )
