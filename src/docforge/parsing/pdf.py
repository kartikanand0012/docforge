"""Cheap checks on a PDF before it reaches a parser."""

import threading

import pypdfium2 as pdfium

from docforge.parsing.base import ParseError

# PDFium is not thread-safe and the API calls this from a thread pool.
_PDFIUM_LOCK = threading.Lock()


def pdf_page_count(data: bytes) -> int:
    """Number of pages. Raises `ParseError` if `data` is not a readable PDF."""
    with _PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(data)
            try:
                count = len(document)
            finally:
                document.close()
        except Exception as error:  # a malformed file can fail in more ways than PdfiumError
            raise ParseError("not a readable PDF") from error
    if count < 1:
        raise ParseError("PDF has no pages")
    return count
