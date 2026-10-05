"""The file formats DocForge accepts, recognised from their bytes.

The name and the type a client sends are never trusted: a `.pdf` that is really a zip is a
zip. Office files are zip containers, told apart by the part that holds their content.
"""

import io
import zipfile
from typing import Literal, get_args

Format = Literal["pdf", "docx", "pptx", "xlsx", "png", "jpeg", "tiff"]
FORMATS: tuple[Format, ...] = get_args(Format)

MEDIA_TYPES: dict[Format, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "tiff": "image/tiff",
}
FORMAT_OF: dict[str, Format] = {media: fmt for fmt, media in MEDIA_TYPES.items()}
OFFICE: frozenset[Format] = frozenset({"docx", "pptx", "xlsx"})
IMAGES: frozenset[Format] = frozenset({"png", "jpeg", "tiff"})
ACCEPTED = "PDF, Word (DOCX), PowerPoint (PPTX), Excel (XLSX), PNG, JPEG and TIFF"

_MAGIC: tuple[tuple[bytes, Format], ...] = (
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
)
# The part that only that kind of office file has.
_OFFICE_PARTS: tuple[tuple[str, Format], ...] = (
    ("word/document.xml", "docx"),
    ("ppt/presentation.xml", "pptx"),
    ("xl/workbook.xml", "xlsx"),
)


def sniff(data: bytes) -> Format | None:
    """The format of `data`, or None if it is not one DocForge accepts."""
    for magic, fmt in _MAGIC:
        if data.startswith(magic):
            return fmt
    if data.startswith(b"PK\x03\x04"):
        try:
            # Only the central directory is read: names, not contents.
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, ValueError, OSError):
            return None
        for part, fmt in _OFFICE_PARTS:
            if part in names:
                return fmt
    return None


def extension(fmt: Format) -> str:
    return "jpg" if fmt == "jpeg" else fmt
