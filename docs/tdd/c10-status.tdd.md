# TDD evidence: C10 status and general documents

Source plan: C10 in `docs/research/06-knowledge-chat-connectors.md` (owner's decisions of
2026-10-03); feature list in `docs/research/08-feature-roadmap.md`.

## Journeys

1. As an uploader, I follow my document stage by stage (stored, converting, parsing,
   extracting, checking, indexing, ready), live, and see why it failed if it did.
2. As an uploader, I find any of my organisation's documents again, filtered by type and
   stage, after it has left the review queue.
3. As an organisation, I upload contracts, SOPs, manuals and slides (Word, PowerPoint, Excel,
   images) for search and chat, with nothing extracted and nothing to review.
4. As an operator, a hostile file costs one document, never the worker or other tenants.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED stages | `f8a7cf3` | stage tests fail |
| GREEN stages, timeline, stream, webhook | `a121203` | stage tests pass |
| Timeline in the review app | `001e3a2` | stage view tested first: 5 failed, then passed; e2e to Ready to chat |
| RED documents list | `caae634` | list tests fail |
| GREEN Documents page | `bb257e9` | list tests pass; e2e lists both documents |
| RED general documents and formats | `f3e8d75` | modules missing; 3 web tests fail |
| GREEN | `9a163e7` | 1,614 Python tests, 92%; LibreOffice tests 30/30 in a Linux container; e2e 8/8 |
| RED review findings | `d59c22d` | 9 Python and 4 web tests fail |
| GREEN | `cda730e` | 1,639 Python tests, 92%; conversion 46/46 with LibreOffice in a container; web 54; e2e 8/8 |

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | Each stage is recorded as it happens, only by the current delivery; ready is announced once | `tests/integration/test_stages.py` | integration |
| 2 | Documents listed newest first, paged, filtered, per tenant; impossible cursors are 422 | `tests/integration/test_document_list.py` | integration |
| 3 | Formats are recognised from bytes, never names | `tests/unit/test_formats.py` | unit |
| 4 | Images become pages: every frame and all together under a pixel limit, transparency on white, scaled to A4 | `tests/unit/test_conversion.py` | unit |
| 5 | Office files with outside links, macros or too much to unpack are refused before LibreOffice; LibreOffice gets only PATH, LANG and a home | `tests/unit/test_conversion.py` | unit |
| 6 | Conversion runs in a child process; time, memory and every process it started are bounded and killed | `tests/unit/test_conversion.py`, `tests/isolation_converters.py` | unit |
| 7 | Real LibreOffice converts DOCX and PPTX, refuses broken files, stops at its time limit | `tests/unit/test_conversion.py` (skipped without LibreOffice; run in a container and in CI) | unit |
| 8 | A general document: no model call, title, pages and words, never in the review queue, found by search | `tests/unit/test_general_pipeline.py`, `tests/integration/test_general_documents.py` | unit, integration |
| 9 | A converted file: stored as uploaded, converted once, page images from its PDF, page limit before storing, the stored PDF wins between deliveries, missing converter retried | `tests/integration/test_general_documents.py` | integration |
| 10 | Retrying and reprocessing shown in the stage column; an old index job cannot make a newer version ready; a stage that cannot be recorded does not retry the work | `tests/integration/test_general_documents.py` | integration |
| 11 | Event streams capped per caller | `tests/integration/test_stages.py` | integration |
| 12 | Stage view, status chip, merged refreshes, search excerpts | `web/tests/unit/stages.test.ts`, `rows.test.ts`, `snippet.test.ts` | unit |
| 13 | A Word document uploaded in the browser is converted, read, indexed, shown and found | `web/tests/e2e/review.spec.ts` | e2e |

## Coverage and known gaps

`make test` 1,639 tests, 92%. See Honest limits under C10 in `docs/progress.md`.
