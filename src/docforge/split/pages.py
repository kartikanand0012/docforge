"""A new PDF made of some pages of another: one child invoice cut from its parent."""

import io

import pypdfium2 as pdfium

from docforge.parsing.base import ParseError
from docforge.parsing.pdf import PDFIUM_LOCK


def cut(pdf: bytes, pages: list[int]) -> bytes:
    """Pages `pages` (1-based, in this order) of `pdf` as a PDF of their own.

    The pages are copied as they are, text layer and images included. Raises `ParseError`.
    """
    if not pages:
        raise ParseError("no pages to cut")
    with PDFIUM_LOCK:
        try:
            source = pdfium.PdfDocument(pdf)
            try:
                if max(pages) > len(source) or min(pages) < 1:
                    raise ParseError("a page outside the document")
                child = pdfium.PdfDocument.new()
                try:
                    child.import_pages(source, [page - 1 for page in pages])
                    out = io.BytesIO()
                    child.save(out)
                    return out.getvalue()
                finally:
                    child.close()
            finally:
                source.close()
        except ParseError:
            raise
        except Exception as error:  # a malformed file can fail in more ways than PdfiumError
            raise ParseError("the pages could not be copied") from error
