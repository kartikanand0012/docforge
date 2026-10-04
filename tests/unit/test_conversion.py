"""Turning an upload into a PDF, so every later stage (parsing, page images, cited boxes) works
on one kind of file. Office files go through LibreOffice in its own process; images through
Pillow, with a size limit."""

import io
import shutil
from pathlib import Path

import pytest
from pypdf import PdfReader

from docforge.conversion import ConversionError, FileConverter, RecordingConverter
from office_files import docx_bytes, image_bytes, pptx_bytes

needs_libreoffice = pytest.mark.skipif(
    FileConverter.find_soffice() is None, reason="LibreOffice is not installed"
)


def pages(pdf: bytes) -> list[tuple[float, float]]:
    reader = PdfReader(io.BytesIO(pdf))
    return [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages]


def test_a_pdf_is_returned_as_it_is() -> None:
    pdf = b"%PDF-1.7 whatever"
    assert FileConverter().to_pdf(pdf, "pdf") is pdf


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_an_image_becomes_a_one_page_pdf_of_letter_size(fmt: str) -> None:
    pdf = FileConverter().to_pdf(image_bytes(fmt, (850, 1100)), fmt.lower())
    assert pdf.startswith(b"%PDF-")
    ((width, height),) = pages(pdf)
    assert (round(width), round(height)) == (612, 792)  # 850 x 1100 pixels at 100 dpi


def test_a_large_photo_is_scaled_to_fit_a_page() -> None:
    """A 12-megapixel phone photo at 72 dpi would be a page 56 inches high."""
    ((width, height),) = pages(FileConverter().to_pdf(image_bytes("JPEG", (3000, 4000)), "jpeg"))
    assert height <= 842 + 1  # no taller than A4
    assert width / height == pytest.approx(3000 / 4000, rel=0.01)


def test_each_frame_of_a_tiff_is_a_page() -> None:
    assert len(pages(FileConverter().to_pdf(image_bytes("TIFF", frames=3), "tiff"))) == 3


def test_an_image_too_large_to_decode_safely_is_refused() -> None:
    converter = FileConverter(max_image_pixels=1000)
    with pytest.raises(ConversionError, match="too large"):
        converter.to_pdf(image_bytes("PNG", (100, 100)), "png")


def test_a_damaged_image_is_refused() -> None:
    with pytest.raises(ConversionError):
        FileConverter().to_pdf(b"\x89PNG\r\n\x1a\n not really", "png")


def test_an_office_file_without_libreoffice_says_so() -> None:
    with pytest.raises(ConversionError, match="LibreOffice"):
        FileConverter(soffice="/nonexistent/soffice").to_pdf(docx_bytes(), "docx")


@needs_libreoffice
def test_a_docx_becomes_a_pdf() -> None:
    pdf = FileConverter().to_pdf(docx_bytes("Goods are counted on arrival."), "docx")
    assert pdf.startswith(b"%PDF-")
    assert "Goods are counted" in PdfReader(io.BytesIO(pdf)).pages[0].extract_text()


@needs_libreoffice
def test_a_pptx_becomes_one_page_per_slide() -> None:
    assert len(pages(FileConverter().to_pdf(pptx_bytes(), "pptx"))) == 1


@needs_libreoffice
def test_a_file_libreoffice_cannot_read_is_refused() -> None:
    with pytest.raises(ConversionError):
        FileConverter().to_pdf(b"PK\x03\x04 not a document", "docx")


@needs_libreoffice
def test_a_conversion_that_runs_too_long_is_stopped() -> None:
    with pytest.raises(ConversionError, match="too long"):
        FileConverter(timeout_seconds=0.001).to_pdf(docx_bytes(), "docx")


def test_libreoffice_gets_no_secrets_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:pw@db/x")
    env = FileConverter.child_environment(Path("/tmp/profile"))
    assert "GEMINI_API_KEY" not in env and "DATABASE_URL" not in env
    assert env["HOME"] == "/tmp/profile"


class Counting:
    def __init__(self) -> None:
        self.calls = 0

    def to_pdf(self, data: bytes, fmt: str) -> bytes:
        self.calls += 1
        return b"%PDF-converted " + data[:4]


def test_recorded_conversions_are_replayed(tmp_path: Path) -> None:
    inner = Counting()
    recording = RecordingConverter(tmp_path, inner)
    first = recording.to_pdf(b"docx bytes", "docx")
    assert RecordingConverter(tmp_path, None).to_pdf(b"docx bytes", "docx") == first
    assert inner.calls == 1


def test_replay_without_a_recording_fails(tmp_path: Path) -> None:
    with pytest.raises(ConversionError, match="no recorded conversion"):
        RecordingConverter(tmp_path, None).to_pdf(b"never seen", "docx")


def test_soffice_is_found_on_the_path_or_in_the_mac_application(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(FileConverter, "MAC_SOFFICE", str(tmp_path / "missing"))
    assert FileConverter.find_soffice() is None
    fake = tmp_path / "soffice"
    fake.write_text("")
    monkeypatch.setattr(FileConverter, "MAC_SOFFICE", str(fake))
    assert FileConverter.find_soffice() == str(fake)
