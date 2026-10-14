"""V2 1.1 part 2: finding the QR codes on an invoice's pages, in a child process with limits."""

import io
from pathlib import Path

import pytest
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from docforge.einvoice.scan import QrScan, QrScanner, isolated_scanner, looks_signed
from docforge.parsing.base import DocumentTooLarge, ParserLimitExceeded

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "einvoice"
JWS = (FIXTURES / "nic_sandbox_qr.txt").read_text().strip()
UPI = "upi://pay?pa=shop@okaxis&pn=Shop&am=100.00&cu=INR"


def pdf(pages: list[list[str]], size: float = 2.0, page_size: tuple[float, float] = A4) -> bytes:
    """A PDF whose pages carry these QR texts, each `size` inches square."""
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer, pagesize=page_size)
    side = size * 72
    for codes in pages:
        document.drawString(72, 750, "TAX INVOICE")
        for index, text in enumerate(codes):
            widget = QrCodeWidget(text)
            left, bottom, right, top = widget.getBounds()
            scale = [side / (right - left), 0, 0, side / (top - bottom), 0, 0]
            drawing = Drawing(side, side, transform=scale)
            drawing.add(widget)
            renderPDF.draw(drawing, document, 72 + index * (side + 36), 450)
        document.showPage()
    document.save()
    return buffer.getvalue()


def test_the_signed_qr_on_page_one_is_found() -> None:
    scan = QrScanner().parse(pdf([[JWS]]))

    assert [(found.page, found.text) for found in scan.found] == [(1, JWS)]
    assert scan.decoder.startswith("zxing-cpp")


def test_a_signed_qr_on_a_later_page_is_found_and_the_search_stops_there() -> None:
    scan = QrScanner().parse(pdf([[], [], [JWS], [], [], [], []]))

    assert [found.page for found in scan.found] == [3]
    assert scan.pages_scanned <= 5


def test_the_last_page_is_read_second_where_some_invoices_print_the_qr() -> None:
    scan = QrScanner().parse(pdf([[]] * 8 + [[JWS]]))

    assert [found.page for found in scan.found] == [9]
    assert scan.pages_scanned == 2


def test_an_invoice_without_a_qr_finds_none() -> None:
    assert QrScanner().parse(pdf([[], []])).found == ()


def test_a_upi_qr_beside_the_signed_one_is_found_too() -> None:
    scan = QrScanner().parse(pdf([[UPI, JWS]]))

    assert {found.text for found in scan.found} == {UPI, JWS}


def test_a_small_signed_qr_is_read_at_a_higher_resolution() -> None:
    # 0.8 inch: below what 300 dpi resolves for a ~900-character QR.
    scan = QrScanner().parse(pdf([[JWS]], size=0.8))

    assert [found.text for found in scan.found] == [JWS]


def test_a_page_too_large_to_render_is_refused() -> None:
    with pytest.raises(DocumentTooLarge):
        QrScanner().parse(pdf([[JWS]], page_size=(14400, 14400)))


def test_only_a_signed_looking_text_stops_the_search() -> None:
    assert looks_signed(JWS)
    assert not looks_signed(UPI)
    assert not looks_signed("a.b.c")


def test_the_scan_runs_in_a_child_process_and_comes_back_typed() -> None:
    scanner = isolated_scanner(timeout_seconds=60, max_rss_mb=1024)
    try:
        scan = scanner.parse(pdf([[JWS]]))
    finally:
        scanner.close()

    assert isinstance(scan, QrScan)
    assert [found.text for found in scan.found] == [JWS]
    assert scanner.spawned == 1


def test_a_scan_over_its_time_limit_is_stopped() -> None:
    scanner = isolated_scanner(timeout_seconds=0.01, max_rss_mb=1024)
    try:
        with pytest.raises(ParserLimitExceeded):
            scanner.parse(pdf([[]] * 9))  # no QR: every page it reads is rendered and searched
    finally:
        scanner.close()
