# TDD evidence: C7 search and certificates of analysis

Source plan: checkpoint C7 in `docs/build-plan.md`; journeys derived during this run.

## Journeys

1. As a reviewer, an invoice whose batch failed its quality tests is held back, and I see which
   result and which limit.
2. As a reviewer, a certificate I cannot trust the checks on (an unclear limit or conclusion)
   is shown as not fully checked, never as passed.
3. As a user, I find a document by its batch, invoice number, GSTIN or a description, and every
   result points to where it is written.
4. As an organisation, search never shows another organisation's documents.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED limits | `bc7a29e` | collection fails |
| GREEN | `de4f53f` | limit tests pass |
| RED synthetic certificates | `4225304` | collection fails |
| GREEN | `5411c04` | generator tests pass |
| RED certificate type | `66c5867` | collection fails |
| GREEN | `49aab65` | spec tests pass |
| RED invoice to certificate | `9787bd5` | link tests fail |
| GREEN | `b003dc1` | link tests pass |
| RED certificate eval | `996a7a0` | collection fails |
| GREEN, then recorded live | `b8313b1`, `4e4a4ff` | 6/6 caught, 0/14 clean flagged |
| RED chunking and embeddings | `60a5ea4` | collection fails |
| GREEN | `ad89d5e` | chunking tests pass |
| RED search | `29a15a6` | collection fails |
| GREEN | `15d51f7`, `1e2d0db` | search tests pass; indexing through the real queue |
| RED search eval | `34ea498` | collection fails |
| GREEN, then recorded live | `9efd01b`, `aef5e84` | hybrid recall@5 1.00, 0 cross-tenant hits |
| RED review findings | `6e8cb75` | the new tests fail |
| GREEN | `9b1bd61` | `make test` 1,480 passed; `make eval` unchanged but one unfiltered figure, which turned out to flip between 0.996 and 0.992 from run to run: equal scores were ordered arbitrarily (fixed in C8, `_TIE_BREAK`); e2e 7 passed |

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | Specifications become limits; compound, partial or unreadable ones are not evaluated | `tests/unit/test_coa_limits.py` | unit |
| 2 | Out-of-limit results are flagged; a compliance claim beside one is a contradiction; unclear conclusions are not evaluated | `tests/unit/test_coa_spec.py` | unit |
| 3 | An invoice is held back by an out-of-limit certificate for its batch; unverifiable certificates say so; batch case and spacing do not matter; another product's batch does not link | `tests/integration/test_coa_link.py` | integration |
| 4 | Every chunk cites its blocks; the summary leaves out unread values and cites only header fields | `tests/unit/test_chunking.py` | unit |
| 5 | Keyword, vector and hybrid modes find the document; codes must match; only the newest version | `tests/integration/test_search.py` | integration |
| 6 | Another tenant's documents are never returned | `tests/integration/test_search.py`, `test_eval_search.py` | integration |
| 7 | The search API is limited per caller and answers 503 without an embedding | `tests/integration/test_search.py` | integration |
| 8 | The committed eval reports reproduce offline and hold their floors | `test_eval_coa.py`, `test_eval_search.py` | integration |
| 9 | A reviewer searches by batch and opens the invoice | `web/tests/e2e/review.spec.ts` | end to end |

## Coverage and known gaps

`make test` 1,480 passed, 93%. Gaps are listed under C7 in `docs/progress.md`.
