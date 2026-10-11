# Plan: V1 Verify on real-world documents, then load and stress test

Planned 2026-10-08 with the ECC planner, from `docs/research/09-real-world-test-corpus.md` and
the owner's order: (1) verify everything up to C16 works as expected with no edge case left
open; (2) load and stress test how many kinds of document DocForge handles, on real-world use
cases; (3) only then a frontend (handover: `docs/frontend-handover.md`, PR #20). Run like the
checkpoints: tests first, ECC reviewers, records. No new product feature is built in V1: what
it finds becomes a fix checkpoint (V1-F1, F2 ...) or a recorded limit.

## Decisions

1. **Verify on the running stack**: a new `tests/system/` layer (`make verify`) drives the
   demo stack (`.deploytest`: Caddy, production settings, container limits, the converter, the
   restricted database role, replayed models) over HTTP. Existing suites stay.
2. **Two stacks**: the demo stack for robustness, memory, body caps and most load; the
   process stack (`ENVIRONMENT=local`) for webhook lag (production refuses a local receiver).
3. **Three load tiers, never mixed**: T1 infrastructure (parses and replies replayed, model
   time simulated); T2 capacity (real parser, model simulated); T3 a small live-Gemini run on a
   budget. T1 and T2 spend nothing. **Budget** (owner, 2026-10-08): the Gemini prepaid balance
   (INR 493.20 that day; the INR 5.76K of "eligible GCP credits" is not counted, as it is not
   confirmed to cover the Gemini API). Estimated V1 spend INR 200-300; the recorder sums the
   tokens it is billed for and stops at a hard cap of INR 400.
4. **k6 for uploads** (open arrival-rate model, right for bursts; a host binary, never
   shipped), **Locust for chat and MCP sessions** (a dev `load` group). `make load` stays.
5. **Only permissive datasets in the repo** (MIT, CC BY 4.0, Apache-2.0, CDLA-Permissive), small
   subsets only; everything else in `data/datasets/` (ignored by git); every dataset recorded in
   `DATASETS.md` (source, licence as read at the source, date, checksum, location).
   Non-commercial sets are not used.
6. **A generated Indian corpus is the main accuracy test** (no permissive set of real GST
   invoices or CoAs exists). A pilot client's samples under NDA are the real test.
7. **Passing means safe, not perfect**: every document is read correctly, sent to a person, or
   refused with a plain reason - never a crash, a 500, or a wrong value accepted. Accuracy is
   measured and reported per variant; safety is gated.
8. **Findings from reading the code are suspicions until a test fails.** Fixes go to fix
   checkpoints after V1.

## 1. Verification pass

`make verify` needs the demo stack up at migration head; it creates two fresh organisations
per run with keys of every role, and uses only public interfaces (HTTP, `/v1/mcp`, SSE,
`docker compose` for kills, `docforge.ops` for counts). Each checklist line is one test.

| Checkpoint | Verify end to end | Open edges: test (T) or limit (L) |
| --- | --- | --- |
| C0 | clean checkout builds and tests; generators byte-identical; no secret in replies or logs | T: GSTIN state code against the real list. L: synthetic GSTINs may be real firms' |
| C1 | born-digital invoice through both upload paths; `12,50` refused | T: hidden text carrying another total is never `accept`. T: `Rs.1,00,000/-` parsed |
| C2 | 20 identical uploads give one document; SIGKILL loses nothing; SIGTERM finishes; storage down 503; Postgres restart; malformed inputs refused | T: worker restarted 5 times during one document (restarts use the attempt budget). T: versions stranded after the queue gives up. T: finished jobs never pruned |
| C3 | every value has a box; 9 seeded defects caught; `accept` only with a matching order | T: a failed match is not retried. T: 20+ documents with one order number. Measure the review rate on correct real documents |
| C4 | good and poor scans; long documents bounded; failures readable | T: pages rotated 90/180 and skewed 7 degrees. T: a scan with a "Scanned with CamScanner" text footer is not OCRed. L: a line split over a page break |
| C5 | queue, correction with PIN, lockout, signature bound to the record | T: page images for all 20 pages. L: no PIN reset |
| C6 | 401 without a credential; foreign ids 404; webhook ids kept; CSV formula-safe | T: row-level security under concurrent load (a cross-tenant probe in every load profile) |
| C7 | keyword, vector, hybrid search; CoA holds; search limits | T: a common word at 100k chunks in other organisations |
| C8 | gate 76/76; spans carry no content; ops-check raises each alert | T: review queue at 1,000 waiting; hybrid search at 200k chunks |
| C9 | non-root images; body cap over 12 MB incl. chunked; memory limits; nightly reset | L: never run on AWS |
| C10 | every stage on the SSE stream; every format reaches `ready` | T: access revoked while a stream is open. T: phone photo with EXIF orientation |
| C11-C12 | checked quotes; abstention; daily limits; scope kept | T: 50 concurrent questions keep an exact daily count |
| H1-H2 | converter isolated (re-checked from inside); sliding minute; renditions deleted | T: limits shared across two API processes under load |
| C13 | paused: only its merged steps' unit tests | L: no live sync |
| C14 | six tools, reader keys, Origin, caps, limits, no text in `agent_calls` | T: two organisations' keys at once during ingestion |
| C15 | fake replies through the real SDKs; replay never calls out | L: no Claude or OpenAI model measured |
| C16 | filters, chain check, export cap, every webhook action and cap | T: the audit list and export at 200,000 entries with `EXPLAIN ANALYZE` |

### Suspected from reading the code (each needs a failing test first)

1. PDF pages are counted with PDFium inside the API process, under one process-wide lock; page
   images are drawn there too: a crashing or slow file could take down or stall an API
   process.
2. PDFium also runs in the worker outside the isolated parser child: a crash kills the worker.
3. The Office zip check trusts declared sizes: an understated zip reaches LibreOffice (its own
   limits must stop it).
4. Legacy `.doc`/`.xls` and password-protected Office files are refused as "not accepted",
   with no mention of a password.
5. A password-protected PDF is refused as "could not be read", without saying why.
6. One extraction queue for all organisations: another organisation's invoice waits behind a
   backfill of up to 1,000.
7. No Retry-After on the "queue full" 503; no per-organisation upload rate.
8. Upload bodies are read whole: concurrency x 10 MB in the API, against its memory limit.
9. The audit chain's lock per organisation serialises every stage of a backfill.
10. The invoice schema: whole-number quantities; no IRN, SAC, cess, e-way bill or TDS fields.

## 2. Real-world corpus

| Dataset | Licence (re-check) | Where | Used for |
| --- | --- | --- | --- |
| katanaml invoices-donut | MIT | 30 committed with recordings; rest in `data/` | non-Indian invoices (no GSTIN: all must go to review) |
| CORD | CC BY 4.0 | 20 committed with attribution | receipts as `general`; as `invoice` they must never be accepted |
| AgamiAI bank statements | Apache-2.0 | 10 committed | Indian multi-page tables, scans, as `general` |
| AgamiAI ITRs | Apache-2.0 | 10 committed | Hindi and English, as `general` |
| CUAD | CC BY 4.0 | `data/` only | long contracts; the 20-page cap |
| DocLayNet | CDLA-Permissive | `data/`, 1,000 pages | layout variety, OCR, throughput |
| govdocs1 subset, SafeDocs sample, Tika/POI corrupt files | various | `data/` only | odd-format and broken-file sweeps (outcome only) |

A fetcher (`python -m docforge.datasets fetch <name>`) works from a manifest of pinned URLs,
checksums, licences and sizes; refuses unlisted sets and non-permissive sets outside `data/`;
stops at an owner-set disk cap (proposed 20 GB). Committed subsets are checked for names and
signatures first.

**Indian generator** (`src/docforge/synth/india/`, seeded, with `label.json` and an expected
outcome): GST tax invoices (CGST/SGST and IGST; HSN and SAC; lakh grouping and `/-`; amounts
in words; pharma and non-pharma with decimal quantities; three new layouts); e-invoices with a
real-format IRN and a signed QR payload (a test key, never NIC's); delivery challans; credit
and debit notes; matching purchase orders; CoAs in four more layouts. Each rendered clean,
scanned at 150 dpi, as a phone photo (with EXIF orientation 6), rotated 90/180, skewed 7
degrees, stamped and signed over the totals, with a handwritten correction, merged 3-5 to a
PDF, with Hindi labels (Unicode, and a legacy-font one), password-protected (user and owner),
with a CamScanner-style footer, and as XLSX and DOCX. About 600 files; a 120-file subset with
recordings committed (under 15 MB).

**What DocForge does today, by document** (to be confirmed by the runs): pharma GST invoice,
purchase order, CoA - supported; non-pharma or service invoice - read with gaps, to review;
e-invoice - read from the printed text, QR and IRN not supported; credit note, delivery
challan, receipt, bank statement - no type of their own (search and chat as `general`; never
accepted as an invoice); merged invoices - not split; over 20 pages - refused; legacy Office,
HEIC, WebP - refused (415); Hindi scans - OCR expected to be poor; legacy-font Hindi - read as
garbage, undetected.

**Scoring**: a `realworld` eval suite (reusing the invoice, CoA and trust scorers) per
rendering: field accuracy, documents fully correct, false accepts, review rate on correct
documents, refusals with a plain message, merged files that should have been split. Dataset
labels become questions for a `realworld-answers` suite (wrong held at 0). Outcome only for the
broken-file sweeps. Each live suite recorded once (T3 budget), then replayed in CI.

## 3. Robustness and adversarial suite

Generated at test time (`src/docforge/synth/adversarial.py`), never committed or downloaded;
run in process and on the stack. For every file: API and worker alive, no container
OOM-killed, the expected outcome, a readable message, and the next ordinary upload still
processed.

| File | Expected |
| --- | --- |
| Flate and nested Flate bombs | parse stopped by the child's memory limit; worker alive; page image bounded |
| Xref-stream bomb, circular `/Prev`, broken xref | counted or 422 within 2 s; no API crash |
| Page tree 100,000 deep; `/Count` lying | 413 or 422 within 2 s |
| 5,000-page PDF / DOCX | 413 in under 1 s / "too long" or page limit after conversion |
| User-password PDF (RC4, AES-128, AES-256) | 422 that says it is password-protected |
| Owner-password-only PDF | processed |
| Zip-bomb DOCX/XLSX (honest and understated headers; overlapping; 10,000+ entries) | refused before LibreOffice, or stopped by the converter's limits; converter alive |
| 1,000,000-row XLSX | "took too long" or page limit |
| Corrupt Office files | "could not be read" |
| Legacy `.doc`/`.xls`, encrypted OOXML, HEIC, WebP | 415 with the accepted list |
| 50,000 x 50,000 PNG; 150-frame TIFF; odd JPEGs and PNGs | refused with a message, or converted |
| PDF with JavaScript, launch actions, attachments | processed; nothing executed or extracted |
| Hidden text layer different from the image | never `accept` with the hidden figures |
| Empty, 1 byte, exactly 10 MB, 10 MB + 1, lying length, chunked over 12 MB | 413, 415 or 422; nothing stored |
| govdocs1 and SafeDocs samples (1,000 each, as `general`) | each refused or terminal with a message; 0 server errors |

**Dependencies**: raise the pypdf floor to `>=6.19.0` (a dev dependency: the service reads PDFs
with pypdfium2 and docling-parse); add `pip-audit` on the lock to CI, covering pypdfium2,
docling-parse and Pillow.

## 4. Load and stress testing

- **Unique files**: k6 appends a marker line to base PDFs and images; office files get a
  unique zip comment (minted beforehand); otherwise uploads are deduplicated.
- **A load pipeline factory** (`docforge.loadtest.pipelines`): T1 replays parses (keyed
  without the marker), model replies and embeddings; T2 runs the real isolated parser with
  replayed or minimal replies. A `SimulatedProvider` under the real retry code sets model time
  (`zero`, `c1` lognormal from C1's p50 14.4 s / p95 26.6 s, `fixed:N`) and injects 429s and
  outages; it logs that replies are simulated and `ops-check` reports it.
- **T3 live**: one worker, 50 generated documents and 20 questions, one upload every 10 s.
- **Where**: the demo stack on the owner's Mac (Docker in Colima); about 10 GB of container
  limits already; each worker adds 3 GB, so 2 workers is the laptop ceiling for T2. Every run
  records the machine, Colima's resources, free disk and git SHA; images pruned first. One
  laptop's numbers are a floor, not a server's capacity.

**Profiles** (volumes assumed until the owner gives client figures: 3,000 documents a month,
about 15 an hour). Size mix: 70% 1-page PDF invoices, 10% 2-5 pages, 5% scans and photos, 5%
XLSX/DOCX, 5% 15-20 pages, 5% near 10 MB.

| Profile | Tier | Shape |
| --- | --- | --- |
| P1 business-day trickle | T1, T2 | 5 organisations, 0.5 a minute each, 60 min |
| P2 month-end / GST spike | T1, T2 | ramp to 10x over 10 min, hold 2 h (T1) or 1 h (T2), drain |
| P3 10k backfill | T1 | one organisation sends 10,000 (backing off on 503); 4 others trickle; measures their delay |
| P4 chat with ingestion | T1 | Locust: 20 users asking, following up, streaming; 2 MCP keys; P2 elsewhere |
| P5 chaos | T1 | during P2: kill a worker every 10 min; restart Postgres; stop MinIO 60 s; kill the converter mid-file; 5-min provider outage |
| P6 stress to break | T1 | ramp until upload p95 > 2 s or errors > 1%: find the knee and what fails first |
| P7 big organisation | T1 | the database seeded to 1M documents, 5M chunks, 1M audit entries (bulk SQL, `generate_series`); P1 on top: list, search, review queue, audit and webhook pages stay within target |
| P8 webhook lag | T1, process stack | P2 with 3 receivers, one slow, one failing |

Every profile runs the cross-tenant probe. No overnight soak (owner: not required); P2's
2-hour hold is sampled for memory and connection growth instead.

**Bigger numbers than one laptop serves** (owner: client volumes unknown; show that more is
possible). Three tools, each answering a different question:

- *Arrival rate*: k6 `ramping-arrival-rate` (P6) raises uploads until the knee; the generator
  mints any number of unique documents (target 50,000 for P3 at full size), so caching cannot
  hide work.
- *Stored volume*: P7 seeds the database at 1M documents, far beyond a year of the assumed
  3,000 a month, and measures the screens and searches at that size.
- *Capacity per worker*: T2 measures pages a minute per worker at 1 and 2 workers; the report
  states the model (documents a day = workers x rate, checked for linearity between 1 and 2)
  and the provider rate limit that caps it, so a client's volume can be sized against it.

**Measured** (a 5-second sampler to JSONL, joined with k6 and Locust summaries into
`evals/load/v1/<profile>.json`): upload p50/p95/p99 and errors by code; search, review queue,
chat (beyond model time) and MCP; time to done per document and per page; pages a minute;
queue depth, oldest job's age, time to drain; API, worker, parser and converter memory;
OOM kills and restarts; database connections, locks not granted, audit-lock waits, deadlocks,
size and job-table growth; provider 429s and retries; webhook lag and pending deliveries;
invariants (0 lost documents, 0 double extractions, 0 cross-tenant hits, 0 unexpected 5xx,
chains verify); recovery time after a kill.

**Targets**: hard - every invariant, 0 OOM kills, 0 native crashes, recovery under 60 s. To
confirm from the first baseline: upload p95 under 500 ms at 10 a second; search p95 under
300 ms during ingestion; chat beyond the model under 1 s p95; webhook lag p95 under 10 s;
database connections under 80%; the P2 queue drained within the working day (T2, 2 workers);
a one-page invoice done in T3 p95 under 60 s; others' time to done during P3 no worse than 2x
their P1. Later runs fail on a 20% regression on the same machine.

## 5. Deliverables

Code: `tests/system/`, `tests/integration/test_robustness.py`, unit tests for every new
module; `src/docforge/synth/india/`, `synth/adversarial.py`, `datasets/`, `loadtest/`,
`evals/realworld.py`; `load/k6/*.js`, `load/locust/chat.py`; `scripts/load_v1.sh`; Make
targets `verify`, `robustness`, `generate-india`, `fetch-datasets`, `load-v1`.

Records: `DATASETS.md`; `tests/fixtures/realworld/`; `evals/baselines/realworld.json`,
`realworld_answers.json`, `robustness.json`; `evals/load/v1/*.json`;
`docs/load-test-results.md` (every profile, environment, measures against targets, the knee,
capacity per worker); `docs/real-world-coverage.md` (document kind x rendering x format against
outcome, with accuracy where there is truth: "how many types of document it handles");
`docs/tdd/v1-real-world.tdd.md`; a V1 entry in `progress.md`.

**Fix checkpoint** for: a crash or OOM kill from one file; any cross-tenant hit; a lost or
duplicated document; a false accept; a 5xx or a refusal without a reason; a stranded version;
another organisation's delay beyond target in a backfill; a query over its timeout at realistic
size. **Recorded limit** for: unsupported types and formats; accuracy below vendor figures on
degraded input that still goes safely to review; laptop-bound throughput; anything needing AWS
or paused work.

## Test-first order

1. Housekeeping: pypdf floor, `pip-audit`, `DATASETS.md`, the `system` marker.
2. The system checklist C0-C16 on the stack: right behaviour locked by a passing test; each
   suspected edge a RED test.
3. Adversarial generator (unit), then the robustness suite.
4. Indian generator (unit), then `realworld` scoring in replay, then the live record run.
5. Dataset fetcher (unit), then adapters and `realworld-answers`.
6. Load tooling (unit), then runs: T1 baseline, P1-P8, T2, T3 (within the budget).
7. Reports, coverage matrix, gate changes, findings sorted; ECC python, security and mle
   reviewers.

## Gate

New replayed checks: `robustness` (cases, 0 unexpected outcomes, 0 server errors, 0 refusals
without reason); `realworld` (0 false accepts, 0 unsupported documents accepted, every refusal
with a reason, accuracy per rendering at its baseline less a stated margin, review rate on
correct documents no worse); `realworld_answers` (0 wrong, correct at its floor, 0
cross-tenant). Load results are machine-dependent: not gated in CI; their invariants make the
runner exit non-zero.

## Needs the owner

Decided 2026-10-08: datasets up to 20 GB outside the repo; real-model spend within the
Gemini prepaid balance (cap INR 400); client volumes unknown, so the bigger-numbers tools
above; NDA samples will be shared; no overnight soak; k6 and Locust approved.

1. NDA samples: put in `data/private/` (ignored by git, never in a recording, report or
   commit; only aggregate scores reported); deleted when the owner says.
2. After the results, which unsupported kinds to add first (credit notes, multi-invoice split,
   QR and IRN, legacy `.doc`/`.xls`, receipts, bank statements, Hindi OCR, a higher page cap).
3. One more dev-only tool: `segno` (QR codes for e-invoices).

## Risks

Synthetic documents may hide real quirks (five layouts, degradations, a pilot's samples);
mirror licences are often wrong (primary sources only, dated); laptop numbers mislead (machine
and tier named, capacity per worker); simulated model time is not the provider's (T3 checks);
bombs run only inside containers with limits; Docker's disk can fill (prune first, datasets
outside Docker); hidden-text and legacy-font documents cannot be caught (stated as limits);
two test sessions at once break each other's databases (runs serialised); a load factory in the
image could be switched on by mistake (it reports itself in `ops-check`).

Estimate: about 14 to 18 working days; fix checkpoints extra, sized from what is found.
