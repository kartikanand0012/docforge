"""Turning an upload into a PDF, so every later stage (parsing, page images, cited boxes) works
on one kind of file. Office files go through LibreOffice in its own process; images through
Pillow, with a size limit."""

import io
import shutil
from pathlib import Path
from typing import cast

import pytest
from pypdf import PdfReader

from docforge.conversion import (
    ConversionError,
    FileConverter,
    IsolatedConverter,
    RecordingConverter,
)
from docforge.formats import Format
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
def test_a_scan_becomes_a_one_page_pdf_of_its_own_size(fmt: str) -> None:
    pdf = FileConverter().to_pdf(image_bytes(fmt, (850, 1100), dpi=100), cast(Format, fmt.lower()))
    assert pdf.startswith(b"%PDF-")
    ((width, height),) = pages(pdf)
    assert (round(width), round(height)) == (612, 792)  # 850 x 1100 pixels at 100 dpi


def test_a_large_photo_is_scaled_to_fit_a_page() -> None:
    """A 12-megapixel phone photo at 72 dpi would be a page 56 inches high."""
    photo = image_bytes("JPEG", (3000, 4000), dpi=72)
    ((width, height),) = pages(FileConverter().to_pdf(photo, "jpeg"))
    assert height == pytest.approx(842, abs=1)  # as tall as A4
    assert width / height == pytest.approx(3000 / 4000, rel=0.01)


def test_an_image_with_no_resolution_fits_a4() -> None:
    ((_, height),) = pages(FileConverter().to_pdf(image_bytes("PNG", (850, 1100)), "png"))
    assert height == pytest.approx(842, abs=1)


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
    with pytest.raises(Exception, match="LibreOffice"):
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


def test_libreoffice_gets_no_secrets_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:pw@db/x")
    env = FileConverter.child_environment(tmp_path)
    assert "GEMINI_API_KEY" not in env and "DATABASE_URL" not in env
    assert env["HOME"] == str(tmp_path)


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


# --- review findings (C10) ---------------------------------------------------------------


def frames_tiff(sizes: list[tuple[int, int]]) -> bytes:
    from PIL import Image

    pages = [Image.new("RGB", size, "white") for size in sizes]
    out = io.BytesIO()
    pages[0].save(
        out, format="TIFF", save_all=True, append_images=pages[1:], compression="tiff_lzw"
    )
    return out.getvalue()


def test_every_frame_of_an_image_is_held_to_the_pixel_limit() -> None:
    """A small first page must not let a huge second one through."""
    tiff = frames_tiff([(10, 10), (2000, 2000)])
    with pytest.raises(ConversionError, match="too large"):
        FileConverter(max_image_pixels=1_000_000).to_pdf(tiff, "tiff")


def test_the_pixels_of_all_frames_together_are_limited() -> None:
    tiff = frames_tiff([(900, 900)] * 3)  # each under the limit, all three over it
    with pytest.raises(ConversionError, match="too large"):
        FileConverter(max_image_pixels=2_000_000).to_pdf(tiff, "tiff")


def test_a_transparent_image_is_put_on_white() -> None:
    from PIL import Image

    from docforge.parsing.raster import render_pages

    image = Image.new("RGBA", (850, 1100), (0, 0, 0, 0))  # transparent: black if flattened
    image.paste((0, 0, 0, 255), (100, 100, 700, 130))
    out = io.BytesIO()
    image.save(out, format="PNG")
    (page,) = render_pages(FileConverter().to_pdf(out.getvalue(), "png"), 72)
    background, bar = page.getpixel((5, 5)), page.getpixel((300, 85))  # greyscale: numbers
    assert isinstance(background, int) and background > 240  # white
    assert isinstance(bar, int) and bar < 20  # the bar still black


def test_libreoffice_gets_only_the_environment_it_needs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("AWS_PROFILE", "S3_ENDPOINT", "SENTRY_DSN", "REDIS_URL"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("LANG", "en_IN.UTF-8")
    env = FileConverter.child_environment(tmp_path)
    assert set(env) <= {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SAL_USE_VCLPLUGIN"}
    assert env["LANG"] == "en_IN.UTF-8"


def office_with(extra: dict[str, str], base: bytes | None = None) -> bytes:
    """A real DOCX with parts added or replaced."""
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(base or docx_bytes()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            if item.filename not in extra:
                target.writestr(item, source.read(item.filename))
        for name, text in extra.items():
            target.writestr(name, text)
    return out.getvalue()


EXTERNAL = (
    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships"><Relationship Id="rId99" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/image" Target="http://169.254.169.254/latest/meta-data"'
    ' TargetMode="External"/></Relationships>'
)


def test_an_office_file_that_links_outside_itself_is_refused() -> None:
    linked = office_with({"word/_rels/document.xml.rels": EXTERNAL})
    with pytest.raises(ConversionError, match="links to"):
        FileConverter(soffice="/nonexistent/soffice").to_pdf(linked, "docx")


def test_an_office_file_with_macros_is_refused() -> None:
    with pytest.raises(ConversionError, match="macros"):
        FileConverter(soffice="/nonexistent/soffice").to_pdf(
            office_with({"word/vbaProject.bin": "x"}), "docx"
        )


def test_an_office_file_that_unpacks_too_large_is_refused() -> None:
    with pytest.raises(ConversionError, match="too large"):
        FileConverter(soffice="/nonexistent/soffice", max_unpacked_bytes=1000).to_pdf(
            docx_bytes(), "docx"
        )


def test_no_libreoffice_is_a_fault_of_the_worker_not_the_file() -> None:
    from docforge.conversion import ConverterUnavailable

    with pytest.raises(ConverterUnavailable):
        FileConverter(soffice="/nonexistent/soffice").to_pdf(docx_bytes(), "docx")


# --- the converter in a child process ----------------------------------------------------


def isolated(**limits: float) -> IsolatedConverter:
    from isolation_converters import CommandConverter

    return IsolatedConverter(CommandConverter, **limits)  # type: ignore[arg-type]


def test_a_conversion_runs_in_a_child_process_and_returns_its_pdf() -> None:
    assert isolated().to_pdf(b"ok", "docx") == b"%PDF-converted"


def test_the_childs_refusal_is_passed_on() -> None:
    with pytest.raises(ConversionError, match="not one we can convert"):
        isolated().to_pdf(b"refuse", "docx")


def test_a_conversion_over_the_time_limit_is_stopped() -> None:
    with pytest.raises(ConversionError, match="took too long"):
        isolated(timeout_seconds=1).to_pdf(b"sleep", "docx")


def test_a_conversion_over_the_memory_limit_is_stopped() -> None:
    with pytest.raises(ConversionError, match="memory"):
        isolated(max_rss_bytes=200 * 1024 * 1024, timeout_seconds=20).to_pdf(b"hoard", "docx")


def test_processes_the_conversion_started_are_stopped_with_it(tmp_path: Path) -> None:
    import psutil

    pidfile = tmp_path / "pid"
    with pytest.raises(ConversionError):
        isolated(timeout_seconds=2).to_pdf(f"grandchild:{pidfile}".encode(), "docx")
    pid = int(pidfile.read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_a_child_that_dies_is_reported() -> None:
    with pytest.raises(ConversionError, match="stopped unexpectedly"):
        isolated().to_pdf(b"exit", "docx")


def test_a_pdf_larger_than_the_limit_is_refused() -> None:
    with pytest.raises(ConversionError, match="too large"):
        isolated(max_output_bytes=1024).to_pdf(b"big", "docx")


def test_a_pdf_upload_never_starts_a_child() -> None:
    pdf = b"%PDF-1.7 as it is"
    assert isolated(timeout_seconds=0.001).to_pdf(pdf, "pdf") is pdf


def test_the_converter_names_its_version_with_libreoffice_and_pillow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import PIL

    monkeypatch.setattr(FileConverter, "find_soffice", classmethod(lambda cls: None))
    version = FileConverter().version
    assert f"pillow={PIL.__version__}" in version and "libreoffice=none" in version


def test_recorded_conversions_have_a_version_of_their_own(tmp_path: Path) -> None:
    assert RecordingConverter(tmp_path, None).version == "recorded"
