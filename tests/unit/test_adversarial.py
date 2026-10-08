"""Hostile and broken files, made at test time (never committed or downloaded): each is what
it says it is, so the robustness suite proves something when it is refused or survives."""

import io
import zipfile
import zlib

import pypdfium2 as pdfium
import pytest
from PIL import Image

from docforge.formats import is_ole, sniff
from docforge.synth.adversarial import MB, Case, cases

ALL = {case.name: case for case in cases()}


def build(name: str) -> bytes:
    return ALL[name].build()


def test_every_case_names_what_it_expects_and_builds_the_same_bytes_twice() -> None:
    assert len(ALL) >= 25
    for case in ALL.values():
        assert case.expect, case.name
        assert case.expect <= {"413", "415", "422", "failed", "processed"}, case.name
        # Encryption salts are random; the 10 MB file is padding and slow to build twice.
        if "password" not in case.name and case.name != "big_exactly_10mb":
            assert case.build() == case.build(), case.name


def test_the_flate_bomb_inflates_a_hundredfold_or_more() -> None:
    data = build("pdf_flate_bomb")
    start = data.index(b"stream\n") + len(b"stream\n")
    end = data.index(b"\nendstream", start)
    inflated = zlib.decompressobj().decompress(data[start:end], 0)
    assert len(inflated) >= 100 * len(data)


@pytest.mark.parametrize("name", ["pdf_password_rc4", "pdf_password_aes128", "pdf_password_aes256"])
def test_a_password_pdf_does_not_open_without_its_password(name: str) -> None:
    with pytest.raises(pdfium.PdfiumError) as error:
        pdfium.PdfDocument(build(name))
    assert getattr(error.value, "err_code", None) == pdfium.raw.FPDF_ERR_PASSWORD


def test_an_owner_password_pdf_opens() -> None:
    assert len(pdfium.PdfDocument(build("pdf_owner_password_only"))) == 1


def test_the_long_pdf_has_five_thousand_pages() -> None:
    assert len(pdfium.PdfDocument(build("pdf_5000_pages"))) == 5000


def test_the_lying_page_count_says_more_than_there_are() -> None:
    data = build("pdf_count_lies")
    assert b"/Count 1000000" in data and data.count(b"/Type /Page ") == 1


def test_the_deep_page_tree_nests_beyond_any_sensible_depth() -> None:
    assert build("pdf_deep_page_tree").count(b"/Type /Pages") >= 10_000


@pytest.mark.parametrize("name", ["docx_zip_bomb", "xlsx_zip_bomb"])
def test_an_office_bomb_inflates_five_hundredfold_or_more(name: str) -> None:
    data = build(name)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        inflated = sum(info.file_size for info in archive.infolist())
    assert inflated >= 500 * len(data)  # deflate cannot pass about 1,030 to 1
    assert sniff(data) in {"docx", "xlsx"}


def test_the_understated_zip_declares_less_than_it_holds() -> None:
    data = build("docx_understated_sizes")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo("word/document.xml")
        declared = info.file_size
        actual = len(zlib.decompressobj(-15).decompress(_member_bytes(data, info)))
    assert actual > 100 * declared


def _member_bytes(data: bytes, info: zipfile.ZipInfo) -> bytes:
    header = info.header_offset
    name_length = int.from_bytes(data[header + 26 : header + 28], "little")
    extra_length = int.from_bytes(data[header + 28 : header + 30], "little")
    start = header + 30 + name_length + extra_length
    return data[start : start + info.compress_size]


def test_the_zip_with_ten_thousand_entries_has_them() -> None:
    with zipfile.ZipFile(io.BytesIO(build("docx_10000_entries"))) as archive:
        assert len(archive.infolist()) > 10_000


def test_older_and_encrypted_office_files_are_ole_containers() -> None:
    for name in ("legacy_doc", "legacy_xls", "encrypted_docx"):
        assert is_ole(build(name)), name


@pytest.mark.parametrize("name", ["heic_image", "webp_image"])
def test_images_not_accepted_are_not_mistaken_for_accepted_ones(name: str) -> None:
    assert sniff(build(name)) is None


def test_the_huge_png_claims_fifty_thousand_pixels_a_side() -> None:
    data = build("png_50000_square")
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    assert (width, height) == (50_000, 50_000) and len(data) < 4 * MB


def test_the_tiff_has_a_hundred_and_fifty_frames() -> None:
    with Image.open(io.BytesIO(build("tiff_150_frames"))) as image:
        assert getattr(image, "n_frames", 1) == 150


@pytest.mark.parametrize(
    ("name", "size"),
    [
        ("empty", 0),
        ("one_byte", 1),
        ("big_exactly_10mb", 10 * MB),
        ("big_10mb_plus_1", 10 * MB + 1),
    ],
)
def test_the_size_edges_are_exact(name: str, size: int) -> None:
    assert len(build(name)) == size


def test_the_exactly_10mb_pdf_still_opens() -> None:
    assert len(pdfium.PdfDocument(build("big_exactly_10mb"))) == 1


def test_active_content_is_present_to_be_ignored() -> None:
    data = build("pdf_javascript_and_attachment")
    for marker in (b"/JavaScript", b"/Launch", b"/EmbeddedFile"):
        assert marker in data
    assert len(pdfium.PdfDocument(data)) == 1


def test_broken_structures_are_broken() -> None:
    for name in ("pdf_broken_xref", "pdf_circular_prev", "pdf_truncated"):
        data = build(name)
        assert data.startswith(b"%PDF-"), name
    assert b"%%EOF" not in build("pdf_truncated")


def test_every_case_is_a_case() -> None:
    assert all(isinstance(case, Case) for case in ALL.values())
