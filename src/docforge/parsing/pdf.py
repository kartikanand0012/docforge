"""Cheap checks on a PDF before it reaches a parser."""

import pypdfium2 as pdfium

from docforge.parsing.base import ParseError


def pdf_page_count(data: bytes) -> int:
    """Number of pages. Raises `ParseError` if `data` is not a readable PDF."""
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        raise ParseError("not a readable PDF") from error
    try:
        count = len(document)
    finally:
        document.close()
    if count < 1:
        raise ParseError("PDF has no pages")
    return count
