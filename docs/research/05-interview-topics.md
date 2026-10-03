# DocForge - Part B: Interview topics this project lets you answer from experience

Compiled 2026-10-01. Sources are 2026 interview-prep guides (not verified first-hand experience posts: I did not find specific Reddit/HN interview-experience threads for document AI; marked below). Frequency marks are MY judgment based on how many independent lists contain the topic: HIGH = in 3+ lists, MED = 2, LOW = 1. No source publishes real frequency statistics (the landedjobs and prachub guides state none).

Sources used:
- [L] https://github.com/landedjobs/rag-engineer-interview-questions (100 Qs, 7 categories; fetched)
- [P] https://prachub.com/resources/ai-engineer-interview-questions-2026-rag-agents-evals-and-production-systems (10 core Qs; fetched)
- [K] https://www.kore1.com/ai-engineer-interview-questions-2026/ (4 interview blocks; fetched)
- [T] https://pub.towardsai.net/7-rag-agent-system-design-questions-you-will-face-in-every-ai-engineer-interview-with-answers-45d31004ffe4 (403; snippet only)
- [C] https://www.coprep.ai/blog/top-ai-engineer-interview-questions-in-2026-llms-rag-agents-and-langchain , https://careery.pro/blog/ai-careers/ai-engineer-interview-questions , https://www.mockingly.ai/blog/rag-system-design-interview (search snippets only)
- Production-problem links in 02-production-problems.md back each "DocForge answer".

## 1. System design
| Question | Freq | DocForge experience to cite | Source |
|---|---|---|---|
| Design a document processing pipeline / RAG over documents end to end | HIGH | Ingest -> OCR fallback -> layout parse -> chunk -> extract -> validate -> store -> search | [P][K][C][T] |
| "Walk me through what happens between a query and an answer for a 200-page PDF" | HIGH | Page-level parsing, chunking, hybrid retrieval, rerank, grounded answer with citations | [K] |
| Walk me through an AI system you shipped | HIGH | DocForge itself, with numbers from your eval set | [P] |
| Sync vs async; where do queues go; how do you scale to N docs/day | HIGH | SKIP LOCKED queue, stateless workers, backpressure, DLQ | [C] [U on exact phrasing]; tianpan pipeline architecture |
| Agent vs deterministic workflow | MED | Fixed pipeline with LLM only at extraction step | [P] |
| Retrieval-time ACLs and enterprise design | MED | Per-tenant RLS | [L] |

## 2. RAG / retrieval
| Question | Freq | Experience | Source |
|---|---|---|---|
| Chunking strategy and trade-offs (size, overlap, tables, structure-aware) | HIGH | Layout-aware chunks, table-as-unit, heading-path metadata | [L][K][C] |
| Hybrid search: BM25 + dense + RRF; why dense alone fails on identifiers | HIGH | Batch numbers, CAS numbers, SKUs; tsvector + pgvector + RRF | [L] |
| Reranking, cross-encoders, latency budget | MED | Optional rerank stage with measured gain | [L][C] |
| Contextual retrieval / orphaned chunks | MED | Anthropic numbers: 5.7% -> 1.9% failure (https://www.anthropic.com/news/contextual-retrieval) | [L] |
| "Answer is wrong though the document exists - debug it" | HIGH | Trace parse -> chunk -> retrieve -> generate; per-stage evals | [P] |
| Embedding choice, dimensionality as RAM bill (N x dim x 4B) | MED | halfvec, dimension trade-offs | [L] |
| Why embedding similarity differs from relevance | MED | Domain jargon in pharma | [K] |
| Query rewriting, freshness, re-indexing via CDC | LOW | Document versions and re-embedding | [L] |

## 3. Evaluation
| Question | Freq | Experience | Source |
|---|---|---|---|
| How do you build an eval set and decide a change can ship? | HIGH | Golden set of labelled documents, CI gate, per-field metrics | [P][K][L] |
| "How do you know it is good? Your number, not your gut" | HIGH | Field-level precision/recall/exact-match, hallucination vs omission rates | [K] |
| Prompt regression detection and prevention | HIGH | Versioned prompts, CI eval on PR, per-model baselines | [K][P] |
| Retrieval metrics (recall@k, MRR, nDCG) and generation faithfulness (Ragas, DeepEval, TruLens) | HIGH | Retrieval eval over labelled queries | [L][C] |
| Can LLM-as-judge be trusted? Calibrate with Cohen's kappa | MED | Judge only for free text; exact-match for numbers | [P][L] |
| Online monitoring, drift, sampling 1-2% for human audit | MED | Review queue sampling | tianpan link |

## 4. LLM reliability
| Question | Freq | Experience | Source |
|---|---|---|---|
| Integrate an LLM behind a reliable application contract (schema validation, retries) | HIGH | JSON schema, constrained output, semantic validators, repair-or-reject | [P] |
| Handle malformed JSON from structured output | HIGH | Validation + bounded retry + DLQ | [K] |
| Hallucination mitigation | HIGH | Source-span grounding, null over guess, cross-field rules, HITL | [C][P] |
| Non-determinism, temperature, model upgrades | MED | Pinned versions, golden-set re-run | [K] |
| Prompt injection from untrusted documents | MED | Hidden-text PDFs, data/instruction separation | [P] (security group) |
| Incident response after model or prompt change | MED | Version stamps, rollback, reprocessing | [P] |
| Fine-tune vs prompt vs RAG | MED | HITL corrections as training data | [K] |

## 5. Data pipelines / async
| Question | Freq | Experience | Source |
|---|---|---|---|
| Idempotency, retries, exactly-once illusions | MED | Content-hash idempotency keys | [U: general backend staple; not found in the AI lists fetched] |
| Dead-letter queues, poison documents | MED | DLQ classification and redrive | same |
| Rate limits (429), backoff with jitter, backpressure | MED | Retry-After, per-tenant concurrency | https://www.getmaxim.ai/articles/handle-429-errors-in-production-llm-applications/ |
| Large-file handling, memory, OOM | LOW | Worker recycling (docling memory issues) | docling GitHub issues in file 02 |
| OCR vs VLM trade-offs, parser selection with numbers | HIGH for document-AI roles [U] | Section 3 of file 02 | file 02 |

## 6. Postgres / pgvector
| Question | Freq | Experience | Source |
|---|---|---|---|
| HNSW vs IVFFlat, tuning ef_search, build params, quantization | HIGH | Measured recall/latency | [L] |
| Filtered ANN recall collapse; iterative scan | MED | Reproduce and fix with 0.8 iterative scan | file 02 section 6 |
| When pgvector is enough vs a dedicated vector DB | HIGH | Scale bands, memory, p99 | [L][C] |
| Partitioning, partial indexes per tenant | MED | Whale-tenant handling | file 02 |
| Queue in Postgres (SKIP LOCKED) | LOW | See above | file 02 |

## 7. Security / multi-tenancy
| Question | Freq | Experience | Source |
|---|---|---|---|
| Secure a RAG or tool-using agent: trust boundaries, authorization | HIGH | RLS, tenant context per transaction | [P][L] |
| Multi-tenant isolation in pgvector (RLS, FORCE RLS, SECURITY DEFINER trap) | MED | Tests that prove cross-tenant reads fail | [L]; file 02 |
| PII/PHI redaction, audit logs, retention | MED | Per-tenant audit log, redaction hooks | [L] |

## 8. Cost / latency
| Question | Freq | Experience | Source |
|---|---|---|---|
| "Quality improved but latency and cost are unacceptable - what do you do?" | HIGH | Route simple pages to cheap path, batch API, caching, smaller models | [P] |
| Token cost tripled - investigate | MED | Per-stage cost telemetry | [K] |
| Semantic caching, latency budget (e.g. sub-800 ms) | MED | Retrieval endpoint timings | [L][C] |
| Build vs buy; stakeholder pushes to ship with weak evidence | MED | Eval gate | [P] |

## Top 10 most frequent (judgment)
1. End-to-end RAG/document pipeline design
2. Chunking strategy
3. Hybrid search (BM25 + dense + RRF)
4. Building eval sets and ship/no-ship gates
5. Hallucination mitigation and structured-output reliability
6. Debugging a wrong RAG answer
7. pgvector vs dedicated vector DB, index tuning
8. Cost/latency optimisation
9. Security and multi-tenancy
10. Prompt/model regression and incident response

## Caveat
These lists are prep-industry content, partly SEO. Frequency claims are inferred, not measured. Live-coding items ("stand up a retrieval endpoint in 40 minutes", "debug a broken agent") appear in [K].

## Learned while building (tagged to the code)

### C8 Operations (2026-10-03)

| Question it answers | What happened in this project | Where to point |
|---|---|---|
| How do you stop a prompt change from shipping a regression? | Recorded replies replayed in CI; floors per report, per field class, and zero wrong values; dataset, model and prompt pinned; on pull requests, no metric worse than the base branch and no floor loosened without a label. Proven with two live-recorded edits: one blocked, one let through. | `evals/gate.py`, `evals/demos/` |
| What surprised you in evals? | A shortened prompt that dropped the rules I thought mattered showed no measurable difference: the reply schema already asks for block ids. A "helpful" prompt that normalised dates lost 56 of them, and the trust layer turned them into missing values, not wrong ones. | `test_eval_gate_demo.py` |
| What can your gate not catch? | A determined author who re-records and lowers floors; run-to-run model variation (one run each); drift on the live model (no scheduled live run yet). 20 documents bound the document failure rate at about 14%. | `docs/progress.md` C8 |
| Debugging slow search | Measured at 50,000 chunks: 180 ms, of which 170 was a per-chunk "newest version" subquery. Moved the fact onto the document; 21 ms. HNSW was never the issue: the planner sorted exactly. | `0012_indexed_version.py` |
| HNSW and multi-tenancy | Postgres filters to the tenant and sorts exactly; forcing the index found 18% of true neighbours on random vectors. Exact keeps full recall; HNSW needs real embeddings at scale to judge. | `evals/vector_scale.py` |
| A flaky eval that was really a bug | One search number flipped between runs: identical documents in two organisations tied, and Postgres ordered ties arbitrarily. I had attributed the change to my own edit. | `_TIE_BREAK` |
| Tracing without leaking data | Spans carry ids, counts, tokens, cost; never content. Review found the leak I had missed: a recorded exception's message and stack (a SQL error's parameters, a validation error's input). FastAPI's own tracing recorded query strings, so it is off. | `telemetry.py::traced` |
| Where does the cost go? | $0.0135 per one-page invoice; 92% is output tokens, because every value carries its block ids. That is the lever, not the prompt. | `evals/baselines/invoice.json` |
| A profile that paid off | The review queue built a regex per value (12,000 per call) and overflowed Python's pattern cache; string search, proven equal on 40,000 cases. | `trust/verify.py::_contains` |
| Running a migration on a busy table | Add the column alone with a lock timeout, fill in batches, build indexes concurrently, add the key NOT VALID and validate after. | `0012_indexed_version.py` |
| Honest load numbers | Model replayed, so the numbers are the system's own; model latency is quoted separately; throughput is named `replay_documents_per_minute`; no p95 from five samples. | `load.py` |

### C7 Search and certificates of analysis (2026-10-03)

| Question it answers | What happened in this project | Where to point |
|---|---|---|
| How do you chunk documents with tables? | One chunk per table row with its column headers, so "batch X" lands on the line that has it; a summary chunk per document for "who sold what"; text chunks for the rest. Each cites the blocks it came from. | `search/chunking.py` |
| Why hybrid and not just vectors? | Measured: vectors alone found 73% of exact-code questions (an embedding does not know 32001 from 32010); keyword alone missed certificates asked about in other words. Fused with reciprocal rank fusion, 100% on this set. | `evals/baselines/search.json` |
| Debugging a wrong answer | Two real misses: Postgres read "NVM/26-27/32001" as a path, so 32001 was never a word (fixed with a second index over the text without punctuation); generic words outranked the code the person typed (codes now must match). Then review found years and "500mg" were being treated as codes. | `search/service.py::_codes` |
| Tenant isolation in vector search | Row-level security and an explicit tenant filter. The eval counts results from the other organisation with and without the filter: 0. Caveat: with an approximate index, filtering after the nearest-neighbour step can lose results, so iterative scans are on; not yet measured at scale. | `_vector`, C8 load test |
| Is your recall number honest? | Partly: the questions are generated from the same documents and the search was tuned on them. It is an upper bound; C8 adds a held-out set. | `evals/search.py` |
| Never pass what you could not read | "NLT 98.0% and NMT 102.0%" was read as its first half, so a result could pass on half a limit. Anything not fully understood is now "not evaluated" and goes to a person. | `trust/limits.py` |
| Recording paid calls for CI | Every embedding is recorded once, keyed by its text; CI replays and fails if a text was never recorded. Changing chunk text means re-recording, which made the summary fix visible. | `search/embeddings.py::RecordingEmbedder` |
| Free-tier quotas | 100 embeddings a minute: indexing waits as long as the server says; a question someone is waiting on retries once and then answers 503. | `GeminiEmbedder._call` |

### C6 Multi-tenant product surface (2026-10-03)

| Question it answers | What happened in this project | Where to point |
|---|---|---|
| Multi-tenancy: app checks or database? | Both. Every query filters by tenant, and Postgres row-level security enforces it under a role that cannot lift it. The whole integration suite runs as that role, so every test also exercises isolation. | `migrations/versions/0008_row_level_security.py`, `db/tenancy.py` |
| How do you pass the tenant to Postgres safely? | A context variable set by each service method; a listener runs `set_config(..., true)` at the start of every transaction, so it is local to the transaction and never stays on a pooled connection. | `db/tenancy.py` |
| Finding the tenant before you know it | Three small owner functions return only a tenant id, from a key prefix, a session prefix or a version id. Nothing else crosses tenants. | `docforge_*_tenant` functions |
| Least privilege, found by review | The app role could truncate the job queue, rename organisations and held UPDATE on append-only tables (refused by triggers, but held). Revoked, with a test per right. | `0010_tighter_rights.py`, `tests/integration/test_privileges.py` |
| Keeping tokens out of the browser | Sign-in sets an HttpOnly, SameSite=Strict cookie; a same-origin route adds the bearer token server-side. A prefetch cache bug and a Secure cookie on plain HTTP both had to be found with a browser test. | `web/src/app/api/` |
| Rate limiting that can be dodged | The per-address limit trusted X-Forwarded-For, which the web server passed straight from the browser. Fixed by counting per reviewer and ignoring client forwarding headers; the real client address must come from the front proxy. | `auth.py::login` |
| Webhooks done properly | Outbox in the same transaction; a deterministic event id so a redelivered job emits nothing new; HMAC over timestamp and body; retries with the same id; secrets derived from a server key, never stored. | `webhooks.py` |
| SSRF | HTTPS and public addresses only; review found DNS rebinding (resolve to check, resolve again to connect), NAT64, and an environment proxy bypass. Now the checked address is the one connected to. | `check_destination`, `_send` |
| A misread library setting | `exponential_wait=10` meant 10, 100, 1000 seconds..., not doubling: the eighth webhook retry would have come months later. | `queue.py` |
| Tamper evidence beyond the database | The chain's head is copied to object storage; a log rewritten with recomputed hashes still chains but no longer ends where the anchor saw it. Honest limit: same storage credentials as the app. | `anchors.py` |

### C5 Review (2026-10-03)

| Question it answers | What happened in this project | Where to point |
|---|---|---|
| Designing human-in-the-loop review | The queue holds only what needs a person, with the reasons. A correction is applied to the model's reply and re-read by the same code and checks, so a person's typo is caught like a model's. | `review/revise.py`, `review/service.py` |
| What does an e-signature need? | The named person, re-authentication at that moment, a stated meaning, and binding to exactly what was signed. A review found the signature was not bound to what the reviewer saw; the client now sends the hash of the record shown. | `ReviewService.sign`, `review/signing.py` |
| Time-of-check to time-of-use | Viewing and signing are two requests; another tab or a reprocess could change the record between them. Fixed by signing against the hash of what was displayed, refused otherwise. | `RecordChanged` |
| Enforcing rules in the database as well as code | No correction after signing, no approval over open checks without a reason, reviewers never deleted or renamed: triggers and constraints, so a script or a bug cannot bypass them. | `migrations/versions/0006_review.py` |
| Account enumeration and lockout | A locked account answered 423, a wrong PIN 401: that told an attacker the email exists. Now one answer. The lockout can still be abused to lock someone out; rate limiting belongs at the edge (C6/C9). | `api/review.py::_errors` |
| A frontend bug only a review found | Opening a second correction form kept the first field's typed value in uncontrolled inputs; saving would have stored it under the other field, signed. Fixed with a `key` on the form. | `web/src/components/Review.tsx` |
| Testing the whole thing | One browser test drives the real API, worker and database on recorded documents: upload, queue, highlight, correct, wrong PIN, sign, draft, audit chain. Runs in CI with no API key. | `scripts/e2e.sh`, `web/tests/e2e` |
| A flaky test harness | The e2e script killed the shell that started the web server, not the server, so the next run hit a stale build. Process groups and a port check fixed it. | `scripts/e2e.sh` |

### C4 Scans and tables (2026-10-03)

| Question it answers | What happened in this project | Where to point |
|---|---|---|
| OCR vs vision model | Kept OCR plus the same text-only model: one pipeline, citations still point at boxes. Measured: 99.96% on a clean scan, 98.06% on a poor one. A vision model is the next thing to compare, on the misreads OCR makes. | `evals/baselines/scans.json` |
| How do you handle skewed scans? | Estimate the angle from the sharpness of the row ink profile, rotate, OCR, then map boxes back. Without it the table rows of a 1.8° scan were scrambled; with it they were intact. | `parsing/raster.py`, `_read_scan` |
| Does OCR error matter if the rules pass? | Yes: 5 poor scans had a misread product name that no rule on the invoice can see. Comparing with the order caught all 5. The metric reported is "wrong value accepted", not only accuracy. | `evals/scans.py` |
| Long tables and output limits | One request per page bounds the reply (about 6,000 tokens a page). Fields given differently by two pages go to review rather than first-wins. | `_ask_by_page`, `merge_pages` |
| Memory and backpressure for big files | Measured rather than assumed: batching cut the peak from 4.2 to 3.4 GB on 301 pages. The real protection is the parser in its own process with limits. | `parsing/isolation.py`, `evals/long_document.py` |
| Isolating native code | Child process started fresh; JSON over the pipe (a review found pickle would let a compromised child attack the parent); secrets removed from its environment; killed on time or memory. | `parsing/isolation.py` |
| A flaky test you tracked down | An intermittent real-parser failure was the model hub dropping a connection when a converter checked its files. Fixed by using the cached models, and an offline setting for production. | `tests/conftest.py`, `PARSER_OFFLINE` |
| Decompression bombs | A PDF can declare a page of any size; rendering it at 200 dpi could take gigabytes. Pages are checked from their declared size first. | `render_pages` |

### C3 Trust layer (2026-10-03)

| Question it answers | What happened in this project | Where to point |
|---|---|---|
| How do you know an LLM extraction is right? | The model returns the printed string and the blocks it came from. Code checks the string is in those blocks as a whole token, runs deterministic rules, and compares the invoice with its order. A value is never corrected silently, only flagged. | `trust/verify.py`, `trust/invoice_rules.py`, `trust/match.py` |
| How do you detect hallucination? | A value that is not in the text it cites is the definition used here. The wrong-batch seeded case is caught this way: the model's reply is altered after the fact and verification flags the field. | `tests/fixtures/seeded/case_009`, `evals/trust.py` |
| Why no confidence score? | A number implies calibration, and there is nothing to calibrate against until reviewers have accepted or corrected fields. So two levels, accept and review, with reasons. Model-reported confidence was not used. | `trust/assess.py` docstring |
| How do you test that checks catch errors? | A seeded-defect set: correct documents with one defect each and a stated list of findings each must produce. 9 of 9 caught, replayed offline in CI with a floor. | `synth/seeded.py`, `tests/unit/test_eval_trust.py` |
| What is your false-positive rate? | Measured, and not flattering: 8 of 20 correct pairs go to review, all because the model cited the neighbouring cell. Reported next to the catch rate, with the two options for reducing it. | `docs/progress.md` C3 |
| Precision and recall trade-off in a real system | Tightening verification (short numbers must be the whole block) first raised flagged values on correct documents from 21 to 51, because the parser merges cells. Replaying the eval showed it at once; the rule was refined and the number returned to 21. | `_supported` in `trust/verify.py` |
| What does "accept" actually claim? | Only that values match their cited text and rules pass. A forged, self-consistent invoice passes, so an invoice with no order on file is review. Found in security review. | `AssessmentDetail.decision` in `documents.py` |
| Substring matching bugs | "20" in "200", "166.40" in "1,166.40", "5" in "Qty 10 Free 5", "05/29" in "05/29/2028", "5.00" in "(5.00)". Each was a way to verify a wrong value; each has a test. | `test_trust_verify.py` |
| Matching records across documents | Order number alone is not identity: another supplier can use the same number. Counterparts must name the same supplier and buyer and be the newest version. Duplicate product lines pair one to one by best agreement. | `_match` in `documents.py`, `match_invoice_to_order` |
| A bug only live data showed | The parser merged the serial number into the product cell, the model copied "1 Paracetamol...", and line pairing with the order failed. Unit tests with hand-built input could not have shown it. | `product_name` in `extraction/normalize.py` |
| How do you add a new document type? | A `DocumentSpec`: raw schema, prompt, normaliser, rules. The purchase order was added this way through the same pipeline, queue and storage. | `extraction/pipeline.py`, `extraction/purchase_order.py` |
| Multi-tenant data integrity in the schema | Every table had a `tenant_id`, but nothing made a child row's tenant agree with its parent's. A database review found it; a test inserted a record under one tenant pointing at another's version and it succeeded. Fixed with composite foreign keys `(id, tenant_id)`, before row-level security depends on it. | `migrations/versions/0005_tenant_consistency.py`, `tests/integration/test_schema_c3.py` |
| An index that would not have been used | The expression index was written with `->`; the ORM emitted a JSON subscript for the same lookup. Postgres treats them as different expressions. Caught by printing the compiled SQL; now one shared expression and a test that reads the plan. | `ORDER_NUMBER` in `db/models.py` |
| Reviewing your own fix | The second review round was run on the fix commit alone and found four more faults, including one introduced by the fix (tie-breaking between duplicate lines). | `docs/progress.md` C3 review |

### C2 Async and durable (2026-10-02)

| Question it answers | What happened in this project | Where to point |
|---|---|---|
| Exactly-once vs at-least-once | Chose at-least-once delivery with an idempotent result. Each delivery takes a turn number at the start and may only write while that turn is current; the result is one transaction. A crash can repeat the model call but never the record. | `process`, `_owned` and `_complete` in `src/docforge/documents.py` |
| How do you make an upload idempotent? | The content hash is the identity, unique per tenant, enforced by the database with `ON CONFLICT DO NOTHING`. The object is stored before the row, because an object without a row is harmless and the reverse is not. | `ingest` in `src/docforge/documents.py` |
| How do you avoid a job without a row, or a row without a job? | The job is inserted on the same connection, in the same transaction, as the document. A test fails the transaction after the enqueue and checks the job is gone too. | `JobQueue.enqueue` in `src/docforge/queue.py`; `tests/integration/test_queue.py` |
| What happens when a worker dies mid-job? | Workers send heartbeats; a job whose worker stopped is put back. Proved with a test that starts a real worker process, kills it with SIGKILL while it holds a job, and starts another. | `requeue_stalled` in `src/docforge/worker.py`; `tests/integration/test_worker_crash.py` |
| A bug your tests missed | The first version passed the kill test but a review found that a worker that only looked dead could come back and overwrite the finished result, or leave the version stuck. The sequential test could not see it. Added the turn number and tests that interleave two deliveries. | `test_a_late_failure_cannot_undo_...` in `tests/integration/test_documents.py` |
| Poison messages | A document that kills its worker every time would have been retried forever, because the retry limit only applied when Python got to handle an error. The attempt count is now taken at the start of each run, so dead workers use up the budget too. | `process` in `src/docforge/documents.py` |
| Retries: what do you retry and what not? | Permanent failures (unreadable file, reply that never fits the schema, hash mismatch) fail at once. Provider and unexpected errors go back to the queue with a growing wait, up to five starts. | `process` in `src/docforge/documents.py`; `src/docforge/queue.py` |
| A review finding that turned out to be wrong | A reviewer reported a deadlock: two code paths taking the document lock and the audit lock in opposite orders. It read as convincing and was "fixed". When asked to prove the fix, running the interleaving against the old code showed no deadlock, and a trace of the SQL showed why: the ORM flushes pending updates before any statement, so the order was already the same. Lesson: reproduce a reported fault before recording it as fixed. The lock order is now explicit and guarded by a test. | `_locked` in `src/docforge/documents.py`; `test_reprocess_and_the_worker_on_one_document_never_deadlock`; `docs/progress.md` |
| What does your system do under load? | Measured with a stand-in for the model: 300 documents through 8 workers at about 150 uploads/s, upload p95 145 ms, every document extracted exactly once, also with half the workers killed mid-run. Stated separately what that does not measure: the parser and the model. | `docs/progress.md` (C2, second verification pass) |
| How would you build a tamper-evident audit log, and what are its limits? | Hash chain per tenant, appended under an advisory lock in the same transaction as the change; the database refuses updates, deletes, truncation and a second successor. Limits stated plainly: someone who can rewrite the table can recompute the hashes, and a chain cut at the end still verifies, until the latest hash is anchored outside the database. | `src/docforge/audit.py`; `src/docforge/migrations/versions/0003_integrity_hardening.py` |
| What should never go in an audit log? | Anything that may need erasing. Filenames can carry personal data and the log cannot be edited, so entries hold hashes, counts and fixed strings only. | `ingest` in `src/docforge/documents.py` |
| Zero-downtime migrations | A review caught a migration that added required columns with no default: fine on an empty database, a failure on one with rows. It now adds, backfills, then constrains, and a test upgrades a database that already holds documents. | `src/docforge/migrations/versions/0002_durable_processing.py`; `tests/integration/test_schema_c2.py` |
| Queue choice | Postgres-backed queue in the same database as the data: transactional enqueue and no extra service, at the price of the queue sharing the database's capacity. | `docs/architecture.md` section 4; `src/docforge/queue.py` |
| Backpressure | Uploads are refused with 503 when too many documents are waiting, and reprocess is refused while a version is in flight, so one caller cannot queue unlimited model calls. | `_pending` and `reprocess` in `src/docforge/documents.py` |

### C1 Walking skeleton (2026-10-02)

| Question it answers | What happened in this project | Where to point |
|---|---|---|
| Design a document pipeline end to end | Parse to positioned blocks, send block text with ids to the model, get back printed strings plus the ids they came from, convert to typed values in code. Each stage sits behind an interface. | `src/docforge/extraction/pipeline.py`, `src/docforge/parsing/base.py`, `src/docforge/llm/base.py` |
| How do you stop a model reformatting or inventing values? | The model only copies text and cites blocks. Dates, amounts and quantities are parsed by code, and a string code cannot read becomes null with an issue, never a guess. Inapplicable tax fields must stay null: 0 of 26 were invented. | `src/docforge/extraction/normalize.py`, `src/docforge/extraction/prompt.py` |
| Structured output reliability | Native structured output gave 20 of 20 schema-valid replies, so the retry library was not needed. One retry with the validation errors is built in and tested. | `InvoicePipeline.extract` in `src/docforge/extraction/pipeline.py` |
| "Your score is 100%. Do you believe it?" | No, not at first. Checked it by a second route that bypasses the scorer, then had it reviewed. The review found the scorer never scored the state code, ignored invented lines in the headline, and accepted citations on the wrong page. Fixed, and added a floor test so a worse baseline cannot be re-recorded silently. Also stated plainly what the set does not cover. | `src/docforge/evals/scoring.py`, `tests/unit/test_eval_run.py`, `docs/progress.md` |
| How do you make evals deterministic and free to run in CI? | Record parser output and model replies keyed by content hash and by model, prompt and schema. CI replays them with no key and no models, and fails if the committed report changes. | `src/docforge/llm/replay.py`, `src/docforge/parsing/cache.py`, `.github/workflows/ci.yml` |
| What happens when you hit a rate limit? | Hit a real one: 20 requests per day per model on the free tier, used up by retries. A per-day quota error is now distinguished from a per-minute one and not retried; the eval records each reply as it arrives and resumes. | `_daily_quota_exhausted` in `src/docforge/llm/gemini.py`, `src/docforge/evals/__main__.py` |
| How do you choose a model? | By measurement on the task: the larger model spent about 6,700 thinking tokens and 40 s per invoice for the same output the lite model gave in 12 s. Model id is pinned, never an alias. | `docs/progress.md` (C1, Departures) |
| Where does your latency go, and how would you cut it? | p95 is 26.6 s against a 15 s goal. About 1.2 s is parsing; the rest is the model writing roughly 5,000 output tokens, because each field is an object with its citations. A compact reply format is the lever. | `evals/baselines/invoice.json` (`usage`) |
| Parser trade-offs you actually saw | Docling placed every one of 2,599 labelled values in a block at the right position, but its table structure was wrong on every invoice: 13 or 14 columns for 15, a merged serial number, misaligned headers in one layout. | `tests/slow/test_docling_parser.py` |
| Prompt injection in documents | Document text is fenced as data and the model has no tools. Review showed the fence could be disguised with zero-width characters; text is now normalised and both fence tags neutralised. Forged block ids in document text are still open until cited text is verified. | `_text` in `src/docforge/extraction/prompt.py` |
| A failure mode that returns success | A scanned PDF has no text layer, so the model received an empty document and returned a valid, all-null record with HTTP 200. Now refused with 422. | `NoTextLayer` in `src/docforge/parsing/base.py` |
| Thread safety in a Python service | The PDF library is not thread-safe and was called from the web framework's thread pool; it is behind a lock, and the parser serialises conversions. Uploads still queue without a limit until jobs become asynchronous. | `src/docforge/parsing/pdf.py`, `src/docforge/parsing/docling_parser.py` |

### C0 Foundations (2026-10-02)

| Question it answers | What happened in this project | Where to point |
|---|---|---|
| How do you build an eval set? | Generated the documents from a seeded model, so the label is the source and the PDF is derived from it. The label also stores the page box of every value, ready for provenance scoring in C3. | `src/docforge/synth/builder.py`, `src/docforge/synth/render.py` |
| How do you know your ground truth is right? | A review found labels held values the page never prints (per-line CGST/SGST, supply type). An extractor would have been marked wrong for not reading something invisible. Each label now lists its unprinted paths, and a test walks every value so none can go unaccounted for. | `DocumentBoxes.unprinted` in `src/docforge/synth/models.py`; `tests/unit/test_synth_render.py` |
| How do you test a calculation without repeating its bug? | The first arithmetic tests re-implemented the builder's formulas, so a shared mistake would pass. Added cases worked out by hand, including a half-paisa tie where CGST and SGST each round up and their sum is a paisa over the rate. | `TestHandComputedArithmetic` in `tests/unit/test_synth_builder.py` |
| A test that passes but proves nothing | The bucket check used an anonymous request and expected 403. MinIO returns 403 for a missing bucket too, so it passed either way. Writing the "missing bucket" case exposed it. | `tests/integration/test_services.py` |
| How do you make generated artefacts reproducible? | Byte-identical PDFs on macOS and Linux needed three things: fixed timestamps and document id, uncompressed streams (bytes otherwise depend on the zlib build), and a fixed month table instead of locale-dependent `%b`. | `_Page.__init__` and `_dated` in `src/docforge/synth/render.py` |
| Idempotency key design | Content hash unique per tenant, not globally, enforced by the database with a format check. Tests cover duplicate within a tenant, same file across tenants, and a malformed hash. | `src/docforge/migrations/versions/0001_core_tables.py`; `tests/integration/test_migrations.py` |
| How can a secret leak without being logged? | The database URL carries the password. It leaked through `repr(settings)` and through the validation error for a bad URL, which echoes the rejected input. | `hide_input_in_errors` in `src/docforge/config.py` |
| Making a replace-in-place operation safe | The generator deleted the old set before building the new one, so a failure left a half-written fixture folder. It now builds everything in memory, then swaps, and writes the manifest last. | `generate_dataset` in `src/docforge/synth/dataset.py` |
| Supply-chain hygiene in CI | A floating action tag did not exist and the vendor's image had been withdrawn. Actions are pinned to commit SHAs and images to digests. | `.github/workflows/ci.yml`, `docker-compose.yml` |
