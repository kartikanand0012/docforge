"""The checks every received file passes before it is stored: by upload or by email (V2 P5).

One place, so the two channels can never accept different files or say different things:
the size limit, the format sniffed from the bytes (`formats.py`; never the name or the type a
sender claims), an older Office or password-protected file, and for a PDF its pages - a
password-protected or unreadable PDF, or one longer than the page limit.
"""

from docforge.formats import ACCEPTED, Format, is_ole, sniff
from docforge.parsing.base import ParseError, PasswordProtected
from docforge.parsing.pdf import pdf_page_count

OLE_MESSAGE = (
    "This is an older Office file (.doc, .xls or .ppt) or one protected by a password. "
    "Save it as .docx, .xlsx or .pptx without a password, or as a PDF, and upload that."
)
PROTECTED_PDF_MESSAGE = (
    "The PDF is protected by a password. Remove the password, or print it to a new PDF, "
    "and upload that."
)
NOT_ACCEPTED_MESSAGE = f"This file type is not accepted. Accepted: {ACCEPTED}."
UNREADABLE_PDF_MESSAGE = "The file could not be read as a PDF."


def too_large_message(max_upload_bytes: int) -> str:
    return f"The file is larger than the limit of {max_upload_bytes} bytes."


class FileRefused(Exception):
    """A file not accepted: the HTTP status an upload answers with, and why, in words."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def check_format(data: bytes, max_upload_bytes: int) -> Format:
    """The file's format. Raises `FileRefused` (413 too large, 415 not accepted)."""
    if len(data) > max_upload_bytes:
        raise FileRefused(413, too_large_message(max_upload_bytes))
    fmt = sniff(data)
    if fmt is None and is_ole(data):
        raise FileRefused(415, OLE_MESSAGE)
    if fmt is None:
        raise FileRefused(415, NOT_ACCEPTED_MESSAGE)
    return fmt


def check_pages(data: bytes, fmt: Format, max_pages: int) -> None:
    """A PDF's pages, counted. Raises `FileRefused` (422 protected or unreadable, 413 too
    long). Other files are made into a PDF in the worker, and counted there."""
    if fmt != "pdf":
        return
    try:
        pages = pdf_page_count(data)
    except PasswordProtected as error:
        raise FileRefused(422, PROTECTED_PDF_MESSAGE) from error
    except ParseError as error:
        raise FileRefused(422, UNREADABLE_PDF_MESSAGE) from error
    if pages > max_pages:
        raise FileRefused(413, f"The document has {pages} pages; the limit is {max_pages}.")


def check_file(data: bytes, *, max_upload_bytes: int, max_pages: int) -> Format:
    """Every check, in the order an upload makes them. Raises `FileRefused`."""
    fmt = check_format(data, max_upload_bytes)
    check_pages(data, fmt, max_pages)
    return fmt
