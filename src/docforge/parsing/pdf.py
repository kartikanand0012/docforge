"""Cheap checks on a PDF before it reaches a parser."""

import threading

import pypdfium2 as pdfium

from docforge.parsing.base import ParseError, PasswordProtected

# PDFium is not thread-safe and the API calls this from a thread pool. Every use of PDFium
# in the package takes this lock.
PDFIUM_LOCK = threading.Lock()


def pdf_page_count(data: bytes) -> int:
    """Number of pages. Raises `ParseError` if `data` is not a readable PDF."""
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(data)
            try:
                count = len(document)
            finally:
                document.close()
        except pdfium.PdfiumError as error:
            if getattr(error, "err_code", None) == pdfium.raw.FPDF_ERR_PASSWORD:
                raise PasswordProtected("the PDF needs a password to open") from error
            raise ParseError("not a readable PDF") from error
        except Exception as error:  # a malformed file can fail in more ways than PdfiumError
            raise ParseError("not a readable PDF") from error
    if count < 1:
        raise ParseError("PDF has no pages")
    return count


_MIN_CHARS = 10  # fewer printed characters than this and the page is treated as an image


def has_text_layer(data: bytes) -> bool:
    """True if every page carries extractable text. A scan, or a file with one scanned page,
    does not. Raises `ParseError` if `data` is not a readable PDF.
    """
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(data)
            try:
                for index in range(len(document)):
                    text_page = document[index].get_textpage()
                    try:
                        printed = "".join(text_page.get_text_range().split())
                        if len(printed) < _MIN_CHARS:
                            return False
                    finally:
                        text_page.close()
                return len(document) > 0
            finally:
                document.close()
        except Exception as error:
            raise ParseError("not a readable PDF") from error
