"""What an upload is, from its bytes: never from the name or the type the client claims."""

import pytest

from docforge.formats import MEDIA_TYPES, sniff
from office_files import docx_bytes, image_bytes, pptx_bytes, xlsx_bytes, zip_bytes


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"%PDF-1.7\n...", "pdf"),
        (docx_bytes(), "docx"),
        (pptx_bytes(), "pptx"),
        (xlsx_bytes(), "xlsx"),
        (image_bytes("PNG"), "png"),
        (image_bytes("JPEG"), "jpeg"),
        (image_bytes("TIFF"), "tiff"),
    ],
)
def test_each_supported_format_is_recognised(data: bytes, expected: str) -> None:
    assert sniff(data) == expected


@pytest.mark.parametrize(
    "data",
    [b"", b"hello", b"PK\x03\x04 broken zip", zip_bytes(), b"<html><body>hi</body></html>"],
)
def test_anything_else_is_not(data: bytes) -> None:
    assert sniff(data) is None


def test_every_format_has_a_media_type() -> None:
    assert MEDIA_TYPES["pdf"] == "application/pdf"
    assert MEDIA_TYPES["docx"].endswith("wordprocessingml.document")
    assert set(MEDIA_TYPES) == {"pdf", "docx", "pptx", "xlsx", "png", "jpeg", "tiff"}
