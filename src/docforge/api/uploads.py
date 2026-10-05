"""Reading and checking an uploaded file. Shared by every endpoint that accepts one."""

import unicodedata
from pathlib import PurePosixPath, PureWindowsPath

from fastapi import HTTPException, UploadFile

from docforge.formats import ACCEPTED, Format, sniff

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024  # boundaries and part headers around the file
_PDF_MAGIC = b"%PDF-"
_MAX_FILENAME = 255


def too_large_message(max_upload_bytes: int) -> str:
    return f"The file is larger than the limit of {max_upload_bytes} bytes."


def safe_filename(name: str | None) -> str | None:
    """The client-supplied name reduced to a base name without control characters.

    It is untrusted: it is stored and echoed back, never used as a path.
    """
    if not name:
        return None
    base = PurePosixPath(PureWindowsPath(name).name).name
    printable = "".join(char for char in base if unicodedata.category(char)[0] != "C")
    return printable[:_MAX_FILENAME] or None


async def read_pdf_upload(file: UploadFile, max_upload_bytes: int) -> bytes:
    """The uploaded bytes, or 413 if too large and 415 if not a PDF."""
    # The server has already received the body; a reverse proxy must cap request size.
    data = await file.read(max_upload_bytes + 1)
    if len(data) > max_upload_bytes:
        raise HTTPException(413, too_large_message(max_upload_bytes))
    if not data.startswith(_PDF_MAGIC):
        raise HTTPException(415, "Only PDF files are accepted.")
    return data


async def read_upload(file: UploadFile, max_upload_bytes: int) -> tuple[bytes, Format]:
    """The uploaded bytes and their format, or 413 if too large and 415 if not accepted."""
    data = await file.read(max_upload_bytes + 1)
    if len(data) > max_upload_bytes:
        raise HTTPException(413, too_large_message(max_upload_bytes))
    fmt = sniff(data)
    if fmt is None:
        raise HTTPException(415, f"This file type is not accepted. Accepted: {ACCEPTED}.")
    return data, fmt
