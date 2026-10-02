# TDD evidence: C1 walking skeleton

Source plan: checkpoint C1 in `docs/build-plan.md`; journeys derived during this run.

## Journeys

1. As an integrator, I upload a born-digital invoice PDF and get back a typed record where every
   field names the blocks it was read from.
2. As an integrator, I get a clear refusal, not an empty record, for a file the service cannot handle.
3. As the person responsible for quality, I run one command and get field-level accuracy, tokens and
   latency for the labelled set, and CI reproduces that report offline.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED parser | `824d86a` | collection fails: `docforge.parsing` missing |
| GREEN parser | `42527bf` | 35 parser tests pass, including real Docling on all 20 invoices |
| RED extraction | `b50b6b6` | collection fails: `docforge.extraction`, `docforge.llm` missing |
| GREEN extraction | `92144b2` | 662 unit tests pass |
| RED API | `032e015` | `test_api.py` fails at import; provider quota tests added and passing |
| GREEN API | `0872da7` | 679 unit tests pass |
| RED eval | `0543bdf` | collection fails: `docforge.evals`, `docforge.parsing.cache` missing |
| GREEN eval | `cc24d1a` | full suite passes; baseline recorded and reproduced offline |
| RED review fixes | `2db4a1a` | new scorer, normaliser, provider and API cases fail |
| GREEN review fixes | `3350d04` | 787 tests pass (748 unit, 13 integration, 26 Docling); CI green |

Three provider tests (daily quota, error status, function-calling off) were written after live calls
exposed the behaviour, and failed before the fix (`3 failed, 13 passed`).

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | Every labelled value on all 20 invoices lies inside a Docling block at its true position | `tests/slow/test_docling_parser.py` | Docling |
| 2 | Parsing the same file twice gives identical blocks; an unreadable file raises `ParseError` | `tests/slow/test_docling_parser.py` | Docling |
| 3 | Dates, months, amounts, integers and place of supply are read by code; ambiguous grouping, mixed separators and non-ASCII digits are rejected | `tests/unit/test_normalize.py` | unit |
| 4 | The prompt carries each block once with its id, keeps table rows together, and cannot have its fence opened or closed by document text, including with invisible characters | `tests/unit/test_prompt.py` | unit |
| 5 | Unreadable values become null with an issue; unknown block ids are dropped and reported; an empty line list is reported | `tests/unit/test_pipeline.py` | unit |
| 6 | A malformed reply is retried once with the errors; two malformed replies raise `ExtractionError` | `tests/unit/test_pipeline.py` | unit |
| 7 | A PDF with no text, too many pages or too much text is rejected before the model is called | `tests/unit/test_pipeline.py` | unit |
| 8 | Gemini requests use structured output, a timeout and an output cap; transient and network errors are retried; an exhausted daily quota is not | `tests/unit/test_llm_gemini.py` | unit |
| 9 | Recordings replay only for an identical model, prompt and schema; a damaged recording is a miss | `tests/unit/test_llm_replay.py` | unit |
| 10 | The API returns the record with document hash, parser and per-call usage; errors map to 413, 415, 422, 502, 503 and a plain 500 without internals | `tests/unit/test_api.py` | unit |
| 11 | The scorer marks wrong, missing, invented and wrongly cited values, scores the state code, and gives failed documents no credit | `tests/unit/test_eval_scoring.py` | unit |
| 12 | The committed baseline is reproduced exactly from recordings and meets the accepted floor | `tests/unit/test_eval_run.py` | unit |

All rows: PASS under `make test` locally and in CI.

## Coverage and gaps

`make test` reports 97% line and branch coverage (threshold 80%).

Not covered by tests:

- The live Gemini call. It was exercised by hand (20 recorded eval calls, one API upload); CI has no key.
- The record branch of the eval CLI (`--mode record`).
- Concurrent uploads against the real Docling parser.
- The scoring tests place citation blocks exactly on the label boxes, so they cannot catch a
  coordinate mismatch with real Docling output; the Docling test (row 1) and the baseline's citation
  figure cover that.
