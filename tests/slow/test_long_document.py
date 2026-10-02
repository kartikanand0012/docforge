"""The long-document measurement, on a short document: real parser, child process, batches."""

import json

import pytest

from docforge.evals.long_document import long_invoice, main, measure
from docforge.parsing.pdf import pdf_page_count

pytestmark = pytest.mark.docling


def test_a_long_invoice_has_at_least_the_pages_asked_for() -> None:
    assert pdf_page_count(long_invoice(3)) >= 3


def test_the_measurement_reports_pages_time_and_peak_memory() -> None:
    pdf = long_invoice(3)

    result = measure(pdf, batch_pages=2, max_rss_mb=8192, timeout_seconds=600)

    assert result["outcome"] == "parsed"
    assert result["pages_parsed"] == result["pages"] == pdf_page_count(pdf)
    assert result["source"] == "text_layer"
    assert isinstance(result["peak_rss_mb"], int) and result["peak_rss_mb"] > 100
    assert isinstance(result["blocks"], int) and result["blocks"] > 100


def test_a_memory_limit_that_is_too_low_is_reported_not_hidden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--pages", "2", "--max-rss-mb", "50", "--timeout-seconds", "600"])

    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "memory limit" in result["outcome"]
