# DocForge progress

One entry per checkpoint: what passed, the measured numbers, and what changed from the plan.

## C7 Search and certificates of analysis (2026-10-03): gate passed

Branch `c7-search`, PR #8 (stacked on C6).

### Gate

| Gate condition | Result | Evidence |
| --- | --- | --- |
| Retrieval recall measured on a labelled question set, with and without the tenant filter | Pass | 124 generated questions over 60 documents in two organisations, through the real upload, extraction, indexing and search services. Recall@5 within the asker's organisation: hybrid 1.00, keyword 0.99, vector 0.87. With every organisation's documents as candidates: hybrid 0.99, keyword 0.98, vector 0.84. Results from the other organisation: 0 in every mode (`evals/baselines/search.json`, `test_eval_search.py` replays it with floors) |
| Out-of-limit CoA results are flagged | Pass | 20 synthetic certificates: 511 of 511 values read; 6 of 6 seeded out-of-limit results caught; 3 of 3 certificates that still claim compliance flagged as contradictory; 0 of 14 clean certificates flagged (`evals/baselines/coa.json`). An invoice whose batch has an out-of-limit certificate is held back (`test_coa_link.py`) |

### Measured

| Measure | Value |
| --- | --- |
| Tests | 1,480 Python (1,148 unit, 290 integration, 42 real-parser), 29 web unit, 7 browser steps |
| Coverage | 93% |

### What was built

- Certificate of analysis as a third document type (`coa`): specifications read into limits
  (not less than, not more than, ranges, "complies"), each result checked against its limit, and
  anything that cannot be read with certainty (a compound or partial specification, a
  conclusion that is neither "complies" nor "does not comply") left as not evaluated rather than
  passed. A conclusion that claims compliance next to a failed result is flagged.
- An invoice is linked to the newest certificate for each of its batches (batch numbers compared
  without case or spacing, and the product must agree). Out of limit holds the invoice back;
  a certificate that could not be fully checked is shown as such and goes to a person.
- Search: each document is cut into a summary, one chunk per table row (with its column headers)
  and text chunks, each citing its blocks. Embeddings (`gemini-embedding-001`, 768 dimensions)
  and full text in Postgres (migration 0011, row-level security as for every tenant table).
  Keyword, vector and hybrid (reciprocal rank fusion) modes; a code in the question (a batch,
  invoice number or GSTIN) must appear in a keyword hit. Only the newest version of a document
  is searched. Indexing is a queue job after extraction.
- `GET /v1/search`, limited per caller (60 a minute by default; vector search is a paid call),
  503 when the embedding service is unavailable. A search page in the review app.
- Synthetic certificates for the invoices' batches; a certificate eval and a search eval, both
  recorded live once and replayed offline in CI.

### Departures from the plan

- pgvector tuning is limited to switching on iterative scans and a wider candidate list. At 60
  documents Postgres sorts exactly and never uses the approximate index, so its behaviour with
  the tenant filter at scale is unmeasured; that is part of the C8 load test.

### Honest limits

- The question set is generated from the same documents and the search was tuned against it
  (codes must match, keyword hits weigh double). The recall figures are an upper bound until C8
  adds a held-out set with near-duplicates, questions that have no answer, and OCR noise.
- Chunks of superseded versions are kept (the record is append-only) and filtered out at query
  time. Deleting an organisation's data on request is not yet built for chunks or anything else.

### Review (ECC rag-pipeline-reviewer, security-reviewer, database-reviewer, python-reviewer)

Fixed, each with a failing test first:

- A specification such as "NLT 98.0% and NMT 102.0%" or "≤ 0.5% (each)" was read as only its
  first half, so a result could pass on half a limit. Anything not fully understood is now not
  evaluated.
- A conclusion worded other than "complies" or "does not comply" counted as compliance.
- A certificate that could not be fully checked showed as within limits; batch numbers written
  in another case or with spaces did not link; the same batch number for another product did.
- A reprocessed document was found twice, once with its old text.
- "Code" detection treated years, strengths and pack sizes (2026, 500mg, 10x10) as codes that
  had to match, so ordinary questions returned nothing.
- The summary chunk printed "None" for unread values and cited every line of the document.
- Search had no per-caller limit, and a missing embedding was a 500. The query is now embedded
  before a database connection is taken, and repeats are cached.
- Two jobs indexing one version could both insert; the second now waits and finds the chunks.
- The search eval used a fixed database name, so two runs at once dropped each other's.

## C6 Multi-tenant product surface (2026-10-03): gate passed

Branch `c6-tenancy`, PR #7 (stacked on C5).

### Gate

| Gate condition | Result | Evidence |
| --- | --- | --- |
| Cross-tenant access tests all fail closed | Pass | Every /v1 route refuses a missing or bad credential (401); another tenant's credential gets 404 on each document route and an empty queue and audit log (`tests/integration/test_auth.py`, run with no ambient tenant). In Postgres, the application's role sees no rows without a tenant and only that tenant's with one, cannot write another tenant's rows, and cannot lift the protections (`test_rls.py`, `test_privileges.py`). The whole integration suite runs its services as that role |
| Webhook retries are idempotent | Pass | A delivery that fails is retried with the same `DocForge-Event-Id`; once delivered it is not sent again; the same event emitted twice is one delivery; events are derived from what they describe, so a redelivered job emits nothing new (`test_webhooks.py`, including a run through the real queue and worker) |

### Measured

| Measure | Value |
| --- | --- |
| Tests | 1,348 Python (1,044 unit, 262 integration, 42 real-parser), 29 web unit, 6 browser steps |
| Coverage | 93% |

No load test of row-level security yet (C8).

### What was built

- API keys for systems and sessions for reviewers (sign-in with organisation, email and PIN), both
  stored only as hashes; roles (integrator, reviewer, admin); the tenant taken from the
  credential on every route; corrections and signatures only by a signed-in person, as
  themselves. Wrong PINs lock a reviewer after five, whether at sign-in or when signing, and are
  also limited per client address.
- Row-level security on every table with a tenant (migrations 0008, 0010). The API and worker
  connect as `docforge_app`, which reads and writes only the current tenant's rows, cannot delete,
  truncate or alter, holds no UPDATE on append-only tables, cannot create or rename organisations,
  and owns nothing. Migrations run as the owner. Three narrow owner functions find a tenant from a
  key prefix, a session prefix or a document version.
- The review screen signs in with an HttpOnly, SameSite=Strict cookie; the browser never holds a
  token. A same-origin route on the web server adds it to API calls. Pages without a session go
  to sign-in. Security headers and a content security policy.
- Webhooks (`document.processed`, `review.signed`, `webhook.test`): written in the same
  transaction as the change, signed (HMAC-SHA256 over timestamp and body, with a per-webhook
  secret derived from a server key and never stored), retried with backoff and the same event
  id, delivered to the address that was checked (HTTPS, public addresses only), with no database
  lock held while the receiver answers. A worker per queue, so webhooks never wait behind a long
  document.
- Export of signed records as CSV (formula-safe) and JSON.
- The audit chain's head anchored to object storage per tenant (`python -m docforge.anchors`)
  and checked on verification: a log rewritten with recomputed hashes is caught.
- `python -m docforge.admin` for organisations and keys; `python -m docforge.db.roles` for the
  application's login.

### Departures from the plan

- Users and roles are reviewers with a role and API keys with a role, not a separate user
  directory or single sign-on.
- CSV and JSON export only signed records.

### Review (ECC security-reviewer, database-reviewer, python-reviewer)

No path across tenants was found. Fixed, each with a failing test first:

- Sign-in had no per-reviewer lockout, and the per-address limit could be dodged with a forged
  X-Forwarded-For, which the web server passed on and the API trusted from localhost. Sign-in now
  counts wrong PINs per reviewer; client forwarding headers are ignored.
- A webhook host could be resolved once to pass the check and again, differently, to connect
  (DNS rebinding); NAT64 addresses reached internal hosts; a proxy in the environment would have
  bypassed the checks. Connections now go to the checked address.
- Retry waits grew tenfold each time (a misread of the queue library's setting): the eighth try
  would have come months later.
- A delivery held a row lock and a transaction open while the receiver answered.
- The application's role could truncate the queue, create or rename organisations, call trigger
  functions, and held UPDATE on append-only tables (the triggers refused it, but the right existed).
- A reviewer could test, and lock, another reviewer's PIN through the signing endpoints.
- The test endpoint ignored which webhook it named; emitting outside a tenant scope recorded
  nothing silently; a stop signal ended only one of the two workers; one storage failure stopped
  anchoring for every later tenant; CSV export altered negative numbers; a backslash in the
  sign-in `next` path was an open redirect.
- Tests: the automatic default-tenant scope could hide a missing scope in an API route; the
  authentication and webhook API tests now run without it.

Recorded, not fixed (C9 deployment):

- The per-address limit is per process and needs the front proxy to pass the real client
  address; a shared limit is needed with several API processes.
- Anchors are written with the same storage credentials as the originals. They catch a rewrite by
  someone with database owner rights but not storage rights. For more, a separate bucket with
  object lock and its own write-only credential.
- The webhook signing key cannot be rotated per webhook through the API yet; receivers must check
  the signature timestamp themselves.
- `review.signed` carries the payment approval draft (payee, amounts) to the receiver by design.
- The content security policy allows inline scripts (a nonce policy is the next step). The
  session cookie's Secure flag trusts the proxy's X-Forwarded-Proto.
- Queue jobs are not tenant-scoped: the application's role can see job arguments (ids) of every
  tenant. Expired sessions and old deliveries are not pruned.
- One integration run out of six failed once, in a test not recorded; the next three runs and the
  full suite passed. Not explained.

### Not verified

- Row-level security under concurrent load; behaviour behind a real reverse proxy.

## C5 Review (2026-10-03): gate passed

Branch `c5-review`, PR #6 (stacked on C4).

### Gate

| Gate condition | Result | Evidence |
| --- | --- | --- |
| A reviewer can resolve a flagged document end to end | Pass | `scripts/e2e.sh` (in CI): a browser uploads an order and an invoice, finds the flagged invoice in the queue, sees the doubtful value outlined on the page image, confirms it with a reason and PIN, is refused on a wrong PIN, signs the approval, gets the payment approval draft, and reopens the document to the signed review. Real API, worker and Postgres; recorded parses and model replies, no key |
| Every action appears in the audit log | Pass | Same test: `document.received`, `extraction.created`, `assessment.created`, `match.created`, `review.corrected`, `review.signed` in order, actor `reviewer:<id>` on the review actions, chain verifies. Failed PINs are logged as `reviewer.pin_failed` (`tests/integration/test_review.py`) |

### Measured

| Measure | Value |
| --- | --- |
| Tests | 1,247 Python (1,011 unit, 194 integration, 42 real-parser), 14 web unit, 5 browser steps |
| Coverage | 94% (Python) |
| End-to-end run | about 7 s for the five browser steps, after the stack is up |

No usability study or timing of real reviewers was done.

### What was built

- Review service (`review/`): a queue of documents that need a person; corrections applied to the
  model's reply and read again by the same normaliser and checks (a reviewer's typing mistake is
  caught like a model's); confirming a value by re-entering it; approval or rejection under a
  signature; a payment approval draft for an approved invoice.
- E-signature: the reviewer re-enters a PIN (scrypt-hashed) for every correction and signature;
  the signature has a fixed meaning per outcome ("I approve this invoice for payment"), binds to
  the hash of the record the reviewer was shown, and covers the record, who, which document and
  version, the reasons and the time. Five wrong PINs lock the reviewer for 15 minutes.
- Migration 0006: reviewers, corrections and reviews (append-only), the model's reply kept with
  each extraction, and database guards: no correction to a signed version, no approval over open
  checks without an override reason, no deleting or renaming a reviewer.
- API: queue, review detail, corrections, signing, page images of the original, an eval summary.
- `web/`: a Next.js review screen (queue, upload, review with the page image and outlined
  sources, correction and signing forms, signed review with the draft) and an evals and cost page.
- `make e2e`, `make web`, `make web-check`; CI runs the web checks and the browser test.

### What the e-signature is and is not

Designed to support an organisation's own electronic-signature controls: each signature names
the person, requires their PIN at that moment, states its meaning, and is bound to a hash of
exactly what was signed. It is not a qualified electronic signature, and until accounts exist
(C6) the PIN is the only identity check.

### Departures from the plan

- Identity is a reviewer row with a PIN, not a user account: accounts and roles are C6.
- Corrections are stored per field in `corrections`, and the corrected record is recomputed, not
  stored as a new extraction. The extraction stays exactly what the model returned.
- No cost figure is shown until model prices are configured: a price not checked against the
  provider's current list is not invented.

### Review (ECC python-reviewer, security-reviewer, database-reviewer, react-reviewer)

Fixed, tests first (each reproduced by a failing test):

- A signature was not bound to what the reviewer saw: a correction from another tab, or a new
  version from reprocessing, could change the record between viewing and signing. Signing now
  sends the hash of the record shown; a different record is refused.
- A superseded version could be corrected and signed while a newer one was being processed.
- When checking an invoice against its order, corrections to the order were ignored.
- The signature hash covered only the record, outcome and meaning; now also the signer, document,
  version, reasons and time. Meanings were free text; now fixed per outcome.
- A locked account answered differently from a wrong PIN, telling an attacker the email exists.
  Failed PINs left no trail.
- The queue took the 200 oldest documents before filtering, so accepted documents could crowd
  out newer ones that needed a person.
- Rules only in code are now also in the database (see migration 0006); a reviewer's name could
  have been edited after they signed.
- A correction path such as `lines[00].qty` was accepted but never matched its field.
- Internal lookup errors were reported as "no such document".
- In the review screen: opening a second correction kept the first field's typed value (it would
  have been saved under the second field); a brief API outage stopped polling for good; a failed
  signature left a stale screen; contrast failures in dark mode and on the page marks; no
  location given to screen-reader users.

Found while testing: the end-to-end script left the web server running after a run (it stopped
the wrapper shell, not the server), so the next run talked to a stale build. It now stops whole
process groups and refuses to start on busy ports.

Recorded, not fixed (C6 or C9):

- No authentication on any route: anyone who can reach the API can read documents and page
  images, and try PINs. The API binds to 127.0.0.1 locally; it must not be exposed before C6.
- No per-IP or global rate limit on PIN attempts; the lockout itself can be used to keep a known
  reviewer locked out.
- No PIN reset or rotation; no content security policy on the web app.
- Rendering page images shares the PDF library lock with extraction in the same process.

### Not verified

- Use by real reviewers; the screen with long multi-page documents in a browser (only one page
  is exercised end to end).
- Accessibility with a screen reader or an automated audit; the review was by reading the code.

## C4 Scans and tables (2026-10-03): gate passed

Branch `c4-scans-tables`, PR #5 (stacked on C3).

### Gate

| Gate condition | Result | Evidence |
| --- | --- | --- |
| Accuracy on the scanned variants is measured and reported next to the clean baseline | Pass | `evals/baselines/scans.json`, replayed offline in CI with floors (`tests/unit/test_eval_scans.py`) |
| A 300-page file does not exhaust memory | Pass | 301 pages parsed in the isolated process, peak 3.4 GB against an 8 GB limit (`python -m docforge.evals.long_document`, one local run) |

### Measured

| Variant (20 invoices) | Fields correct | Fully correct | Sent to review by own checks | Wrong value accepted by own checks | ...and also agreeing with its order |
| --- | --- | --- | --- | --- | --- |
| Clean PDF | 2472 / 2472 | 20 | 8 | 0 | 0 |
| Good scan (150 dpi) | 2471 / 2472 | 19 | 11 | 0 | 0 |
| Poor scan (110 dpi, 1.8°, blur, noise) | 2424 / 2472 (98.06%) | 4 | 12 | 5 | 0 |

Poor-scan errors: 23 of 48 are a letter in a product name ("Tabiets", "Insufin"), the rest
addresses, pack sizes, free quantities read as blank, two discounts read as "25" for "2.5", one
batch number ("E00589" for "EO0589"). Citations stay above 99% on every variant.

| Long documents | Result |
| --- | --- |
| 3 invoices, 45 / 80 / 120 lines, 2 / 2 / 4 pages | 45 and 80 lines fully correct; 120 lines: one line lost where the parser merged two table rows, every later line shifted; the checks sent it to review |
| Model calls | 8 (one per page); output about 6,000 tokens per page; p50 140 s per document |
| 301-page born-digital invoice, 155,000 blocks | 24 min, peak 3.4 GB in batches of 10 pages; 23 min, 4.2 GB unbatched |
| 31-page scan | 7 min (about 14 s a page), peak 4.0 GB |

Tests: 1,188 passed (996 unit, 150 integration, 42 real-parser), coverage 94%.

These are synthetic documents with known degradations. Real scans (photos, stamps, handwriting,
fax artefacts, rotated pages) are not measured.

### What was built

- Scanned variants of the 20 invoices in two profiles (`synth/scans.py`), with label boxes moved
  by the page rotation; the rotation maths is checked against pixels.
- OCR path in `DoclingParser`: a PDF without a text layer on every page is rendered at 200 dpi,
  each page's skew estimated from its text lines and corrected, read by OCR, and its boxes mapped
  back to the page as uploaded. Without deskewing, the crooked scan's table rows were scrambled.
- Conversion in page batches; long invoices and orders in the generator (`--multipage`).
- Page-by-page extraction for documents of more than one page, merged in code; one-page
  requests are unchanged, so existing recordings still replay.
- `IsolatedParser`: the parser in a spawned child process with a time limit, a memory limit,
  recycling after 50 documents, JSON replies and no credentials in its environment. A limit
  exceeded fails that document with a reason the uploader can read.
- Scan, multi-page and long-document measurements (`make eval`, `python -m docforge.evals.long_document`).

### Departures from the plan

- Table-by-table extraction became page-by-page: simpler, and it bounds each reply.
- Batching was expected to be what keeps memory bounded. Measured, Docling already streams pages:
  batching lowered the peak from 4.2 to 3.4 GB, and time did not change. The isolation limits are
  what protect the worker.
- No per-block OCR confidence: Docling does not expose it in the form used here.

### Review (ECC python-reviewer and security-reviewer)

Fixed, tests first:

- A page with a huge declared size would have been rendered into gigabytes of pixels. Pages are
  now checked from their declared size before rendering and refused above 60 megapixels.
- Replies from the parser process were unpickled; a process taken over through a native-code bug
  could have run code in the parent, which holds the credentials. Replies are JSON now, and the
  child starts with secrets removed from its environment.
- Merging pages took the first value printed on any page, so a carried-forward subtotal on page 1
  would have become the grand total. Pages that disagree on a field now send the document to
  review; licence numbers from several pages are all kept.
- A retry per malformed page could double the cost of a long document; retries are now counted
  per document (two).
- An interrupted exchange could leave a reply in the pipe to be read as the next document's;
  `close()` waited for a parse of up to 15 minutes. Both fixed.
- Boxes mapped back from a crooked scan were the upright box around the turned line, several
  lines tall; they now keep their size around the moved centre.
- The scan eval counted a document whose extraction failed as "sent to review", and an order that
  could not be read aborted the run.
- `has_text_layer` counted spaces as text.

Found while testing: a real-parser test failed intermittently because a new converter asked the
model hub whether its files were current. Tests now use the models on disk; the service can do the
same with `PARSER_OFFLINE=true` after `make models`.

Recorded, not fixed:

- The text-layer decision trusts any text layer of 10 or more printed characters per page. A PDF
  whose hidden text differs from its visible image would be read from the hidden text, and a scan
  with a text footer would not be OCRed. A cross-check (OCR a sample, compare) is the fix; until
  then the review screen (C5) must show the rendered page, not the extracted text.
- Pages rotated by 90 or 180 degrees are not detected; skew beyond 5 degrees is not corrected.
- The memory limit is polled every 0.2 s; fast allocation can outrun it. The deployed worker
  needs a container memory limit as the hard stop (C9).
- One slow document holds the worker's parser for up to the time limit.
- No per-tenant limit on documents or pages (C6).
- A line item split across a page break would become two partial lines; the rules would flag the
  incomplete one, but nothing merges them.

### Not verified

- Real scans, phone photos, rotated pages.
- OCR and parser throughput on server CPUs or GPUs; all timings are one laptop CPU.
- The isolated parser under the real worker for long periods (only tests and single runs).

## C3 Trust layer (2026-10-03): gate passed

Branch `c3-trust-layer`, PR #4.

### Gate

| Gate condition | Result | Evidence |
| --- | --- | --- |
| Every field links to a page box | Pass | Each extracted value carries the blocks it cites, and the stored assessment gives each one its page and box. `GET /v1/documents/{id}/assessment` serves them (`tests/integration/test_assessment.py`). On the 20 invoices, 99.15% of values cite a block that covers the labelled position (unchanged from C1) |
| Seeded errors (wrong batch, bad arithmetic, expired stock) are all caught | Pass | 9 seeded cases, each a correct pair with one defect and a stated list of findings it must produce. Run through the real parser and the live model, then replayed offline in CI: 9 of 9 cases caught, 12 of 12 expected findings (`evals/baselines/trust.json`, `tests/unit/test_eval_trust.py`) |

### Measured

| Measure | Value |
| --- | --- |
| Seeded cases caught | 9 of 9 (bad line amount, bad grand total, expired stock, PTR above MRP, bad GSTIN, quantity over order, rate over order, short free quantity, wrong batch) |
| Expected findings produced | 12 of 12 |
| Findings beyond those expected on seeded cases | 4, all citations of a neighbouring cell for the grand total or round-off |
| Correct pairs accepted with no review | 12 of 20 |
| Values flagged on correct documents | 21 of about 2,470; every one is a correct value whose citation points at a neighbouring block |
| Invoice extraction baseline | Unchanged: 2472 of 2472 printed fields correct |
| Model calls for the trust eval | 58 (40 clean documents, 18 seeded), recorded once, replayed in CI |
| Tests | 1,084 passed (909 unit, 149 integration, 26 Docling) |
| Coverage | 95% |

The 12 of 20 figure is the important limit. No correct document had a wrong value, but 8 of 20
would have gone to a person because the model cited the cell next to the right one. On this data
the review rate for correct documents is therefore 40%, which is too high to sell as
"straight-through". Two ways to bring it down, neither applied yet: accept a value found in a
block adjacent to the cited one in the same table row (weaker evidence, needs a decision), or
improve the citations themselves (prompt, or table-by-table extraction in C4).

These numbers are from 20 synthetic, born-digital, single-page pairs in two layouts. They say
nothing yet about scans or real documents.

### What was built

- `trust/verify.py`: each value's printed string must appear in the text of the blocks it cites,
  as a whole token. Statuses: verified, not in cited blocks (with where it was found instead), no
  citation.
- `trust/rules.py`, `trust/invoice_rules.py`: 13 deterministic rules with an id, a version and a
  severity. A rule with a missing input reports "not evaluated", not "failed".
- `trust/match.py`: invoice against purchase order: order number, parties, dates, and per line
  quantity, rate, pack and free quantity against the ordered scheme.
- `trust/assess.py`: one assessment per version: accept or review, with reasons.
- A second document type, the purchase order, through the same generic pipeline
  (`DocumentSpec`). Adding a type is a schema, a prompt, a normaliser and its rules.
- Stored `assessments` and `matches` (migration 0004), immutable like extractions, each written
  to the audit log. Migration 0005 ties every record's tenant to its version's tenant. Matching runs automatically whichever of the two documents arrives second.
- `synth/seeded.py` and `tests/fixtures/seeded/`: the seeded-defect set. `evals/trust.py`: the
  trust eval. `make eval` replays both evals offline.

### What "accept" means

`accept` means every value was found in the text it cites and no rule failed. It does not mean
the document is genuine: a forged but self-consistent invoice passes its own checks. For that
reason an invoice with no order on file is `review`, and the API reports `match_status`
(`match`, `mismatch`, `no_counterpart`) next to the decision.

### Departures from the plan

- No numeric field confidence. The plan said "field confidence"; what is built is two levels
  (accept, review) with reasons. A number would not be calibrated against anything until there
  are reviewer outcomes to calibrate it on (C5 onward).
- Results are stored as one JSON document per version (`assessments.data`, `matches.data`), not
  as the `fields`, `validations` and `discrepancies` tables in the architecture. Per-field rows
  come with review in C5, when a field needs its own status and history.
- Only the invoice-to-order match. The certificate of analysis is C7.

### Review (ECC python-reviewer and security-reviewer, two rounds; database-reviewer)

No secret exposure and no path across tenants was found. The first round found that "accept"
could be earned too easily. Fixed, tests first:

- A short number such as "5" verified against any block containing a 5. It must now be the whole
  cited block, or one of the numbers in a block that holds only numbers (the parser sometimes
  merges two numeric cells).
- A number verified against its negative or bracketed form, and could be assembled from pieces
  of two cited blocks. An empty value could verify.
- Two lines of the same product (two batches) were paired through a dictionary, so one was lost.
  Names differing only in case or spacing did not pair.
- An ordered line missing from the invoice, and a value that could not be compared, were
  warnings. Both now need review.
- A blank free quantity against an ordered scheme was not reported.
- A missing batch number on a line was not an error. A blank discount stopped the line
  arithmetic from being checked.
- The counterpart was found by order number alone, so another supplier's order with the same
  number matched, and a superseded version could be the counterpart.
- Page text that looks like a block id (`[b9]`) could pass for one in the prompt.

The second round, on the fixes, found four more, each reproduced with a failing test first:

- "05/29" verified inside "05/29/2028".
- With two lines of one product at the same quantity, pairing took the first, so a pair listed
  in the opposite order produced two false rate discrepancies.
- An order line with no readable product was silently skipped.
- After an order was read again and no longer matched, the invoice still showed the comparison
  with the old reading.

The database review (run on migration 0004 and the new queries) found that nothing made a
record's `tenant_id` agree with the tenant of the version it points at, in the C2 tables as well
as the new ones. Reproduced with a failing test, then fixed in migration 0005: composite foreign
keys from every record to its version and from every version to its document; a match must pair
an invoice version with a purchase-order version; an expression index for the order-number
lookup and an index on documents by type. The first spelling of the lookup in code would not
have used the new index (SQLAlchemy wrote it as a subscript, the index used `->`); the
expression is now defined once and a test checks the plan. The review found no race in matching
and no fault in the migration's upgrade or downgrade.

Recorded, not fixed:

- The application still connects as the database owner, so it could disable the immutability
  triggers. A restricted role is C6, as already planned.
- If the matching step fails after the extraction has committed (a database error, say), the
  error is logged and the match is not retried. Reprocessing either document repeats it. A
  reconcile job belongs with operations in C8.
- Documents extracted before C3 have no assessment until they are reprocessed; the API answers
  that there is none yet.

- A short number sharing its cell with a label ("Qty: 1") is not verified. It goes to review,
  which is the safe direction, at the cost of review load on layouts that print labels in cells.
- Within a block of merged numeric cells, verification cannot tell which number is which. A
  swapped discount and tax rate would be caught by the arithmetic rules, not by verification.
- The counterpart search looks at the 20 newest documents with the same order number and filters
  by party in code. More than 20 would hide the real one; the result is review.
- Line pairing is greedy, not an optimal assignment.

### Not verified

- Scanned or real documents (C4 and later).
- That the 40% review rate on correct documents holds beyond these two layouts.
- Behaviour with many documents sharing an order number.

## C2 Async and durable (2026-10-02): gate passed

Branch `c2-async-durable`, PR #3.

### Gate

| Gate condition | Result | Evidence |
| --- | --- | --- |
| Same file uploaded twice yields one document | Pass | Service, queue and API tests; live run: second upload returned HTTP 200, `created: false`, same id, no new job |
| Killing a worker mid-job loses nothing | Pass | `tests/integration/test_worker_crash.py`: a real worker process is killed with SIGKILL while holding a job; a second worker finishes it; exactly one extraction exists |
| Audit chain verifies | Pass | `tests/integration/test_audit.py`; live run: `GET /v1/audit/verification` reported the chain consistent |

### Measured

| Measure | Value |
| --- | --- |
| Tests | 907 passed (766 unit, 115 integration, 26 Docling) |
| Coverage | 96% |
| Live run, upload to stored extraction | 26 s for a 10-line invoice (Docling parse plus an 18 s model call), one model call |
| Upload response | Returned before processing started (HTTP 202) |
| Recovery after a killed worker | Not timed. The test allows 40 s with a 2 s stalled-worker timeout and passes; the default timeout is 30 s |

No load or throughput measurement was made in this checkpoint.

### What was built

- `POST /v1/documents` stores the original in object storage under its content hash, records the
  document, and enqueues a job, in one database transaction. A worker (`make worker`) extracts.
- `GET /v1/documents/{id}`, `/extraction`, `/audit`, `POST /reprocess`, `GET /v1/audit/verification`.
- Versions: each processing run is a version. Extractions are immutable in the database.
- An append-only, hash-chained audit log, one chain per tenant.
- The queue is Procrastinate in the same Postgres database, as the architecture chose.
- Document types are registered by name (`PIPELINE_FACTORY`), following the decision to keep the
  product general. Only `invoice` is registered so far.
- The C1 endpoint `POST /v1/extractions` remains as a stateless preview that stores nothing.

### How processing stays correct when things go wrong

Delivery is at-least-once. Each delivery takes a turn number when it starts and may write its
result only while that turn is still current, so a delivery that was overtaken writes nothing.
The result is written in one transaction. The turn count is also the budget: after five starts the
version is failed, whether the earlier attempts ended in an error or in a dead worker. The cost of
this design is that a model call can be repeated after a crash; the extraction is still recorded
once.

### Departures from the plan

- **Parse output is stored as one JSON document per version**, not as `pages` and `blocks` tables.
  Those arrive with provenance queries in C3.
- **The audit claim in the architecture was reworded.** It said the application role has no update
  or delete grant; that role does not exist until C6. See the architecture, section 5.
- **No Object Lock on originals yet.** Local MinIO only; the worker does check each original
  against its recorded hash before extracting.
- **Document status is the status of the newest version.** A failed reprocess shows `failed` while
  the earlier extraction is still served.

### Review (ECC python-reviewer, database-reviewer and security-reviewer)

No secret exposure. The reviews found faults in the failure paths that the first version of the
gate tests did not exercise. Fixed in this checkpoint:

- A late or duplicate delivery could overwrite a finished version or leave it stuck.
- A document that crashed its worker would have been retried without limit.
- Large inserts ran while the per-tenant audit lock was held. (The same review reported a deadlock
  between reprocess and the worker. That turned out to be wrong; see "Second verification pass".)
- Any exception other than a provider failure dropped the job and stranded the version.
- Migration 0002 failed on a database that already had rows, and its downgrade silently discarded
  audit records.
- Nothing in the database stopped two audit entries sharing a parent.
- Unlimited reprocess and upload were a cost amplifier; reprocess is now refused while a version
  is in flight and uploads are refused when 1,000 documents are waiting.
- The worker extracted whatever was at the storage key without checking its hash.
- Filenames were written into the permanent audit log.
- The audit wording claimed more than a plain hash chain delivers.
- `PIPELINE_FACTORY` could name any importable function; in production it is restricted to this
  package.

Deferred, with the checkpoint that should pick each up:

| Item | Checkpoint |
| --- | --- |
| Separate migration and application database roles; application role with insert and select only on the audit log | C6 |
| Latest audit hash recorded outside the database, or a keyed hash | C6 |
| Composite foreign keys so a child row's tenant must equal its parent's; tenant in job arguments for row-level security | C6 |
| Per-tenant rate limits and quotas; cache for chain verification, which reads the whole chain per call | C6 |
| A sweeper for versions left in flight after the queue gives up (for example a database outage longer than all retries) | C8 |
| Removing finished jobs from the queue table | C8 |
| Model runs of a failed version are not stored | C8 |
| S3 Object Lock, server-side encryption, TLS-only, least-privilege storage principal: required before any real document is stored | C9 |
| Deploy restarts use up the same attempt budget as real failures | C8 |
| Applying constraints with `NOT VALID` then `VALIDATE` for large tables | When a database with real volume exists |

### Verification pass before merge (2026-10-02)

Run against real processes (API, workers, Postgres, MinIO) on a scratch database, with a stand-in
pipeline unless stated.

| Check | Result |
| --- | --- |
| 20 simultaneous uploads of one file | One 202, nineteen 200, one document, one version, one job |
| 19 different files at once, two workers | All 20 documents extracted once each; every version ran once; chain consistent with 60 entries |
| 10 simultaneous reprocess requests | One accepted, nine refused with 409 |
| Both workers killed with SIGKILL while each held a job | A new worker finished both; one extraction each, two attempts each |
| SIGTERM to a worker holding a job | It finished the job, then exited with status 0 |
| A document whose worker is killed on every attempt | Failed after 5 starts with "Processing was interrupted too many times." |
| An audit entry edited with the triggers disabled | Verification reported the chain inconsistent at that entry |
| Object storage stopped during an upload | Request failed with no document row and no job; the same upload succeeded once storage was back |
| Postgres restarted under a running API and worker | Both carried on; the next uploads were accepted and processed |
| Malformed input: empty file, magic bytes only, truncated, password-protected, 11 MB, 21 pages, wrong field, non-multipart body, unknown type, hostile filename, malformed id | Each refused with 413, 415 or 422; nothing stored |
| Real pipeline: a PDF with no text | Version failed with "The PDF has no text layer."; no model call |
| Real pipeline: an invoice, then reprocess | Both versions succeeded; header, totals, every batch and amount matched the label |

Found and fixed in this pass:

- Storage or database being unavailable answered 500 "Internal error."; it now answers 503 with a
  "try again later" message, and the worker treats unavailable storage as retryable.

Observed, not a fault:

- Reprocessing the same invoice with the same model gave identical values but two different
  citations (the label cell in one run, the value cell in the other). The model is not perfectly
  repeatable at temperature 0; C3's check of values against cited text covers this.
- A worker sent SIGTERM in its first second, before it installs its handlers, exits at once. It
  holds no job at that point.
- A blank `doc_type` form field is treated as omitted and defaults to `invoice`.

Added to the test suite from this pass: a crash after the model call but before the write,
simultaneous reprocess and upload requests, and unreachable storage.

### Second verification pass (2026-10-02)

Closing the items the first pass left open. Stand-in pipeline unless stated.

| Check | Result |
| --- | --- |
| 300 documents, 8 workers, 16 concurrent uploaders | All accepted; about 150 uploads/s; upload latency p50 100 ms, p95 145 ms, max 204 ms; all processed in 3.6 s (84 documents/s) |
| The same with 4 of the 8 workers killed with SIGKILL one second in | All 300 processed in 8.6 s; none lost |
| Exactly-once across both runs | 600 versions, 600 extractions, no version ran twice, no duplicate extraction |
| Audit chain after both runs | Consistent, 1,800 entries, recomputed in 29 ms |
| Recovery with default settings (heartbeat 10 s, stalled after 30 s), one surviving worker | Job finished 28 s after the kill; expect roughly 20 to 45 s depending on when the last heartbeat landed |
| Real pipeline: worker killed with SIGKILL during the Gemini call | Killed before the model replied; a second worker finished it 56 s later; one extraction, correct against the label, two attempts recorded |
| Reprocess racing the worker on one document, 15 rounds | No database error; at most one version in flight; chain consistent. Now a test in the suite |

These throughput figures measure everything except the model and the parser: the stand-in pipeline
returns in milliseconds. With the real pipeline a worker handles roughly four invoices a minute.

**The deadlock reported in review was not real.** The database review said reprocess and the worker
took the document lock and the audit lock in opposite orders. Running that interleaving against the
code from before the change (commit `bc70056`, 40 rounds, 800 reprocess calls) produced no deadlock,
and a trace of its SQL showed why: the ORM sends pending row updates before any statement, so the
document row was already locked before the audit lock was requested. Both paths took the locks in
the same order all along. The explicit ordering added in this checkpoint is harmless and makes the
order visible, but it did not fix a fault, and the earlier summary that said it did was wrong.

Found and fixed in this pass:

- The API answered 503 for unavailable storage without logging the cause. It now logs it.

### Not verified

- Workers in separate containers or on separate machines. Three attempts to run workers in
  containers failed for a reason unrelated to DocForge: the local Docker VM could not download
  Python packages. Heartbeats and audit timestamps use the database clock, which limits the
  exposure to clock differences; a real check belongs with the first container image in C9.
- Throughput and latency with the real parser and model under sustained load. Needs a paid model
  tier; planned for C8.
- More than eight workers.

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
