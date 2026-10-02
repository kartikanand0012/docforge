# TDD evidence: C4 scans and tables

Source plan: checkpoint C4 in `docs/build-plan.md`; journeys derived during this run.

## Journeys

1. As an integrator, I upload a scanned invoice and get the same kind of record, with boxes on
   the page as I uploaded it.
2. As a reviewer, I am not told a misread value is fine: OCR errors are caught by the checks or
   the order match, or counted where they are not.
3. As an integrator, I upload a long invoice and every line comes back, or the document is
   flagged.
4. As an operator, one hostile or huge file costs one document, not the worker.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED raster, skew, box rotation, scans | `85a5633` | collection fails: modules missing |
| GREEN | `af7a09b` | unit 937 passed |
| RED OCR path, box mapping, batching | `bf7b0f3` | `DoclingParser` takes no `batch_pages` |
| GREEN | `acd0578` | real-parser tests 39 passed |
| Scan eval (tests and code together) | `a91c5da` | `test_eval_scans.py` 4 passed |
| RED long pairs | `be22a2a` | collection fails |
| GREEN | `614ee48` | unit 954 passed; existing fixtures byte-identical |
| RED page-by-page extraction | `1a94852` | 7 failed, 2 passed |
| GREEN | `9c0a1fb` | unit 963 passed |
| RED isolated parser | `ee07747` | collection fails |
| GREEN | `413658f` | `test_isolation.py` 10 passed |
| Parser-limit reasons (tests and code together) | `9b85229` | 1123 unit and integration passed |
| Scan eval recorded live | `147606d` | floors in CI |
| Multi-page eval recorded live | `ec55289` | replay test passes |
| Review fixes (tests first, one commit) | `cd753a2` | 1145 passed |
| Model-hub flake | this branch | `make test` passed twice, 1188 tests |

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | Box rotation agrees with where the pixels go; turning keeps a box's size | `test_raster.py` | unit, pixel-checked |
| 2 | Skew is estimated within 0.25° for -3.2° to 1.8°; blank page 0 | `test_raster.py` | unit |
| 3 | A page too large to render is refused before pixels are made | `test_raster.py` | unit |
| 4 | Scans have no text layer, keep page size, are reproducible per seed; labels move with the page | `test_synth_scans.py` | unit |
| 5 | A scan is read by OCR; batch number, grand total and last amount sit where the ink is on both profiles; the crooked scan's table keeps its rows | `tests/slow/test_docling_scans.py` | real parser |
| 6 | A long document keeps every page in order; batching does not change the result | `tests/slow/test_docling_scans.py` | real parser |
| 7 | Long invoices split over pages with repeated headers; values print where their boxes say | `test_synth_multipage.py` | unit |
| 8 | One request per page with only that page's blocks; merged result; one-page requests unchanged | `test_pipeline_paged.py` | unit |
| 9 | Pages that disagree on a field send the document to review; retries limited per document | `test_pipeline_paged.py` | unit |
| 10 | Parser in a child: errors keep their type; crash, timeout, memory and close each fail one document and the next works; no secrets in the child; JSON not pickle | `test_isolation.py` | unit, real processes |
| 11 | A parser-limit failure is stored with its reason | `test_documents.py` | integration |
| 12 | Committed scan, multi-page and invoice reports replay offline; floors on accuracy; no wrong value passes both checks and order match | `test_eval_scans.py`, `test_eval_run.py` | unit, recorded |
| 13 | The long-document measurement reports pages, time and peak memory, and reports a limit hit | `tests/slow/test_long_document.py` | real parser |

## Coverage and known gaps

`make test`: 1,188 passed, coverage 94%. Known gaps are listed under C4 in `docs/progress.md`.
