# DocForge progress

One entry per checkpoint: what passed, the measured numbers, and what changed from the plan.

## C1 Walking skeleton (2026-10-02): gate passed

Branch `c1-walking-skeleton`, PR #2.

### Gate

| Gate condition | Result | Evidence |
| --- | --- | --- |
| Born-digital invoice in, schema-validated JSON out, through the API | Pass | Live upload of `pair_004/invoice.pdf` to `POST /v1/extractions` returned the correct record; API tests |
| Eval script runs on the 20 documents and records a baseline | Pass | `make eval-record` (live), `evals/baselines/invoice.json`; `make eval` reproduces it offline in CI |

### Baseline

Model `gemini-3.5-flash-lite`, prompt `invoice-v1`, schema `invoice-1`, Docling 2.132.0 on CPU.
20 synthetic invoices, two layouts, one page each.

| Measure | Value |
| --- | --- |
| Printed fields correct | 2472 of 2472 |
| Documents fully correct | 20 of 20 |
| Invented line items | 0 |
| Values given for tax fields that are not printed | 0 of 26 |
| Values citing a block that covers their true position | 99.15% (21 misses) |
| Model calls | 20, no retries |
| Tokens per document | 2,091 in, 5,166 out |
| Model latency per document | p50 14.4 s, p95 26.6 s |
| Docling parse time per document | about 1.2 s on CPU (measured separately, 19 documents) |
| One live API request, end to end | 20.8 s, of which 13.5 s in the model |

**What this number does and does not show.** The set is clean, born-digital, single-page and drawn
from two templates. The prompt was written after looking at Docling's output on these same
layouts (it warns about misaligned table headers), so this is not a held-out test. A perfect score
here says the pipeline works end to end and the scorer has something to regress against. It says
nothing about scans, real invoices, or unfamiliar layouts. An independent check, bypassing the scorer and the normaliser,
found all model strings equal to the printed text.

**The p95 latency misses the architecture's 15 s goal.** Most of the time is output: each field is
returned as an object with its block ids, about 5,000 tokens per invoice. A more compact reply
format is the obvious lever and belongs with the cost work in C8.

**The 21 citation misses** are values where the model cited the label cell ("Round Off") or a
neighbouring cell. C3's check of each value against its cited block's text is what catches these.

### What Docling does with these invoices

Every labelled value (2,599 boxes) lies inside a block at its true position. Its table structure
is less faithful: it finds 13 or 14 columns where there are 15, merges the serial number into the
next cell, and in layout B the header row is misaligned with the data. The model resolved all of
it on this set. On two invoices the totals block was read as a second table.

### Departures from the plan

- **Model.** The plan pinned `gemini-3.8-flash` with `gemini-3.7-flash` as fallback. Both returned
  503 "high demand" throughout. `gemini-3.6-flash` is capped at 20 requests a day on the free tier
  and was used up by retries. `gemini-3.5-flash` works but spent about 6,700 thinking tokens and
  40 s per invoice. `gemini-3.5-flash-lite` returned the same correct output in 12 s with no
  thinking, so it is the development model.
- **Instructor not used**, as agreed: no reply failed schema validation in 20 documents.
- **Thinking tokens** are recorded separately from output tokens.

### Review (ECC python-reviewer and security-reviewer)

No critical findings. Fixed in this checkpoint:

- The scorer did not score the printed state code, did not count invented lines, accepted a
  citation on any page, and credited failed documents with correct nulls. A re-recorded worse
  baseline would have passed silently; a floor test now prevents that.
- A PDF without a text layer returned 200 with an all-null record; it is now refused with 422.
- `12,50` was read as 1250, and digits of other scripts were accepted.
- The provider had no timeout or output cap, and network errors surfaced as 500.
- pypdfium2 was called from several threads without a lock.
- The prompt fence could be disguised with zero-width characters.
- The uploaded filename was echoed back unsanitised.

Deferred, with the checkpoint that should pick each up:

| Item | Checkpoint |
| --- | --- |
| No authentication on the API; it must stay bound to localhost | C6 |
| No admission limit: uploads queue on the parser lock and each holds a worker thread | C2 (async jobs) |
| A request without a declared length is spooled to disk before the size check; needs a proxy cap | C9 |
| Docling runs in-process with no time or memory limit | C4 (parser worker isolation) |
| Cited block text is not compared with the value; a forged or wrong block id passes | C3 |
| Text such as `[b7]` inside a document looks like a block id to the model | C3 |
| Hidden text (white, tiny, off-page) is extracted like visible text | C3 |
| The recording key does not include generation settings such as temperature | C8 |
| Parenthesised negatives, long month names and `Jun-26` are read as null | C3 |
| Docling model weights are fetched without a pinned revision; their licences need a per-model check | C9 |
| No dependency vulnerability scan in CI | Before the repository goes public |
| Whether any checksum-valid synthetic GSTIN belongs to a real firm | Before the repository goes public |

### Constraint found: free-tier quota

`gemini-3.6-flash` allows 20 requests per day per model on the free tier. The limits of the other
models are not known; `gemini-3.5-flash-lite` served 22 requests in this session without a limit
error. The eval is resumable: replies are recorded as they arrive, a daily-quota error stops the
run with a clear message, and re-running continues from where it stopped.

### Proposed plan changes (for decision, not yet applied)

From the market research shared on 2026-10-02. Most of it is already planned (confidence and review
in C3 and C5, webhooks in C6, eval gate and cost in C8). Three changes are worth deciding:

1. **Public eval and cost page, earlier.** Move a read-only page showing accuracy by field class,
   tokens and latency per document from C8/C9 into C5, built from `evals/baselines/*.json`. The
   report format already holds what it needs.
2. **Extract-then-act in the demo.** Make the C5 demo end with an action: a matched invoice posts
   an approval draft, a mismatch opens a review task and fires a webhook. This pulls the webhook
   part of C6 forward.
3. **Vertical packs.** The research suggests BFSI, healthcare and legal packs. This conflicts with
   the agreed pharma-first scope. Recommendation: keep pharma as the lead demo and add one second
   document type after C5 only if the schema-per-document-type design makes it cheap.

### Not verified

- Behaviour under concurrent uploads.
- The free-tier limits of `gemini-3.5-flash-lite`.
- Token prices: tokens are recorded, cost in currency is not computed.

## C0 Foundations (2026-10-02): gate passed

Branch `c0-foundations`, PR #1.

### Gate

| Gate condition | Result | Evidence |
| --- | --- | --- |
| `make up` passes on a clean machine | Pass | GitHub Actions run on a fresh `ubuntu-24.04` runner; also locally on macOS (Colima) |
| `make test` passes on a clean machine | Pass | Same CI run: 575 tests |
| 20 synthetic invoice/PO pairs with labels exist | Pass | `tests/fixtures/synthetic/pair_001` to `pair_020`, seed 20261002 |

### Measured

| Measure | Value |
| --- | --- |
| Tests | 575 passed (562 unit, 13 integration) |
| Line and branch coverage | 99% (threshold 80%) |
| Lint and types | ruff and mypy strict, clean |
| CI duration | about 1 minute |
| Fixture set size | 1.3 MB for 40 PDFs and 20 labels |
| Reproducibility | Regenerated fixtures are byte-identical on macOS arm64 and the GitHub Linux runner |

### What was built

- Python 3.12 project managed by uv; `Makefile` targets `up`, `down`, `migrate`, `test`, `lint`, `generate`.
- Docker Compose with Postgres 16.15 + pgvector 0.8.7 and MinIO, bound to 127.0.0.1, both pinned by digest.
- Alembic migration `0001`: `vector` extension, `tenants`, `documents` (unique content hash per
  tenant), `document_versions`.
- Settings module with secrets masked in `repr` and in validation errors, and a production guard
  against the local default credentials.
- GSTIN check-character validation.
- Synthetic generator: seeded pharma invoice and purchase-order pairs in two layouts, rendered with
  ReportLab. Each label records the page box of every printed value and lists the values that are
  not printed.
- CI workflow running the same `make` targets, with actions pinned to commit SHAs.

### Departures from the plan

- **MinIO image.** MinIO withdrew its official images from Docker Hub and Quay. Compose uses
  Chainguard's MinIO build pinned by digest. `architecture.md` still says "MinIO locally", which
  remains true.
- **boto3 added now, not in C2.** MinIO answers anonymous requests with 403 for both existing and
  missing buckets, so the bucket check needs a signed request.

### Review (ECC python-reviewer and security-reviewer)

No critical findings. Fixed in this checkpoint:

- Labels held values that are never printed (per-line CGST/SGST/IGST, supply type, state codes).
  Each document now lists its unprinted paths and a test walks every label value.
- Generation deleted the old set before building the new one; it now builds in memory first.
- PDF bytes depended on the zlib build and month names on the locale; both removed.
- Arithmetic tests mirrored the implementation; hand-computed cases added.
- `DATABASE_URL` could leak its password through `repr` and through validation errors.
- Actions were tag-pinned and the pgvector image was a moving tag.

Deferred, with the checkpoint that should pick each up:

| Item | Checkpoint |
| --- | --- |
| `documents.status` and `doc_type` are free text; add constraints once the state machine exists | C2 |
| GSTIN state code is not checked against the list of real states | C3 |
| HSN codes and GST rates in the synthetic catalogue are not checked against the tariff | C3 |
| Synthetic drug-licence number format is invented | C3 |
| No seeded errors (wrong batch, bad arithmetic, expired stock) in the set yet | C3 |
| No tax summary table or multi-page invoices in the layouts | C4 |
| Generator will delete any `pair_NNN` folder inside the `--out` directory | Accepted; documented in `--help` |
| Secret scanning and `SECURITY.md` | Before the repository goes public |

### Not verified

- The Gemini API key in `.env` has not been called. C0 makes no model calls.
- `make up` was not run on Windows.
