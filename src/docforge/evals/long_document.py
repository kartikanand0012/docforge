"""`python -m docforge.evals.long_document`: parse one very long invoice and report the cost.

Measures the parser alone, in its child process: seconds, blocks, and the most memory the
child held. No model is called. The file is an invoice whose line table is long enough to
fill the requested number of pages; `--scan` turns it into images first, to measure OCR.
"""

import argparse
import json
import time
from collections.abc import Sequence
from functools import partial
from importlib.metadata import version

from docforge.parsing.docling_parser import DoclingParser
from docforge.parsing.isolation import IsolatedParser
from docforge.parsing.pdf import pdf_page_count
from docforge.synth import DEFAULT_SEED
from docforge.synth.builder import build_pair
from docforge.synth.render import render_invoice
from docforge.synth.scans import PROFILES, scan_pdf

_LINES_PER_PAGE = 36  # layout A after the first page


def long_invoice(pages: int) -> bytes:
    """An invoice of at least `pages` pages."""
    pair = build_pair(201, DEFAULT_SEED, line_count=max(pages * _LINES_PER_PAGE - 10, 1))
    return render_invoice(pair.invoice, pair.layout).pdf


def measure(
    pdf: bytes, batch_pages: int, max_rss_mb: int, timeout_seconds: float
) -> dict[str, object]:
    parser = IsolatedParser(
        partial(DoclingParser, batch_pages=batch_pages),
        name=DoclingParser.name,
        version=version("docling"),
        timeout_seconds=timeout_seconds,
        max_rss_bytes=max_rss_mb * 1024 * 1024,
    )
    started = time.monotonic()
    try:
        parsed = parser.parse(pdf)
        outcome: dict[str, object] = {
            "outcome": "parsed",
            "source": parsed.source,
            "pages_parsed": len(parsed.pages),
            "blocks": len(parsed.blocks),
        }
    except Exception as error:  # the measurement reports a failure, it does not hide it
        outcome = {"outcome": f"{type(error).__name__}: {error}"}
    finally:
        seconds = time.monotonic() - started
        peak = parser.peak_rss_bytes
        parser.close()
    return {
        "pages": pdf_page_count(pdf),
        "file_mb": round(len(pdf) / 1024**2, 2),
        "batch_pages": batch_pages,
        "seconds": round(seconds, 1),
        "peak_rss_mb": round(peak / 1024**2),
        "memory_limit_mb": max_rss_mb,
        **outcome,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m docforge.evals.long_document")
    parser.add_argument("--pages", type=int, default=300)
    parser.add_argument("--batch-pages", type=int, default=10)
    parser.add_argument("--max-rss-mb", type=int, default=8192)
    parser.add_argument("--timeout-seconds", type=float, default=7200.0)
    parser.add_argument("--scan", action="store_true", help="measure the OCR path")
    args = parser.parse_args(argv)

    pdf = long_invoice(args.pages)
    if args.scan:
        pdf = scan_pdf(pdf, PROFILES["scan_good"], seed=DEFAULT_SEED)
    result = measure(pdf, args.batch_pages, args.max_rss_mb, args.timeout_seconds)
    print(json.dumps(result, indent=2))
    return 0 if result["outcome"] == "parsed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
