# DocForge architecture (v0.1, for sign-off)

2026-10-01. Draft for Kartik's approval; no code exists yet.
Inputs: `docs/research/01`–`05`. Where this document departs from a research recommendation, it says so.

## 1. What DocForge is

A document-intelligence service for regulated, document-heavy operations. It turns PDFs and scans into
validated, structured records where every value can be traced to the place on the page it came from,
and nothing AI-extracted becomes a record until a rule or a person accepts it.

**Demo scope (v1)**

| Workflow | Documents | What the system decides |
| --- | --- | --- |
| Primary: distributor invoice match | Pharma distribution invoice + purchase order | Do batch, expiry, MRP/PTR, quantity and free-quantity scheme, GST/HSN, drug licence and line arithmetic agree? |
| Secondary: CoA check | Certificate of analysis + specification | Is each result within its limit (NMT/NLT/range)? Do batch and expiry match the invoice? |

Out of scope for v1: batch manufacturing records, pharmacovigilance, label management, ERP connectors beyond CSV/JSON export.

**Wording rule.** We say "designed to support 21 CFR Part 11 / Annex 11 workflows". We never say
compliant, validated, certified, zero-retention or 100% accurate.

## 2. Design principles

1. **Text comes from the parser, meaning comes from the model.** OCR/layout produces the characters and
   coordinates. The LLM only selects and structures; it points at block IDs and never retypes a number
   unchecked.
2. **Proposed until accepted.** Every AI value has status `proposed`. It becomes `accepted` through a
   passing rule set plus threshold, or a human action. No silent auto-approval on the QA workflow.
3. **Null over guess.** A missing field is a valid answer and routes to review.
4. **Everything is versioned and attributable.** Original bytes, parse output, extraction, schema, prompt
   and model are all versioned and recorded on each run.
5. **One datastore until measurements say otherwise.** Postgres holds records, search, queue and audit.
6. **Every external dependency sits behind an interface.** Parser, OCR, LLM, embeddings and object store
   are swappable, so a client can run it in their own VPC with their own model contract.

## 3. Pipeline

```
upload ─▶ ingest ─▶ classify ─▶ parse ─▶ extract ─▶ validate ─▶ match ─▶ score ─▶ route
           │          │          │          │           │          │        │        ├─▶ accepted
           hash,      doc type,  blocks +   fields +    rules      invoice  field    └─▶ review queue ─▶ human accept / correct / e-sign
           store      digital    bboxes     block refs  (code)     ↔ PO     confidence
           original   vs scan                                      ↔ CoA
                                    └─▶ chunk + embed ─▶ hybrid search
```

| Stage | What it does | Key decisions |
| --- | --- | --- |
| Ingest | SHA-256 of bytes, store original unchanged, create document + version rows | Hash is the idempotency key per tenant. Page and size caps reject oversized files early. |
| Classify | Document type and born-digital vs scanned, per page | Cheap heuristics first (text layer present, text-layer vs OCR disagreement as an injection check), small model only if needed. |
| Parse | Produces `blocks`: text, type (paragraph, table cell, key-value), page, bounding box, OCR confidence | Docling for born-digital. Scans go through the OCR adapter. Runs in subprocess workers recycled every N documents (parser memory leaks). |
| Extract | One schema per document type (Pydantic). LLM returns each field with the block IDs it came from | Per-document-type prompts. Line items extracted table-by-table, not whole-document. Structured output for shape; our validators for truth. |
| Verify | Every numeric and identifier field is compared to the text of its cited blocks | Mismatch means the field is flagged, never silently corrected. |
| Validate | Deterministic rules in code | GSTIN checksum, HSN format, dates (expiry after invoice date), line arithmetic, tax totals, scheme quantity ("10+1"), PTR ≤ MRP, drug licence format, CoA result vs limit (a limit or conclusion not fully understood is "not evaluated", never passed). |
| Match | Invoice ↔ PO ↔ CoA on batch, product, quantity, price | Produces a discrepancy list with severity. |
| Score | Field confidence from OCR confidence, block-alignment, rule results and cross-document agreement | Thresholds per field class, set from the eval set, not guessed. |
| Route | Accept, or create a review task | A random 1–2% of accepted documents also goes to audit review (silent-error detection). |
| Index | Chunk by layout blocks (a summary, one chunk per table row with its headers, text), embed, write `tsvector` | A queue job after extraction. Hybrid search with reciprocal rank fusion; a code in the question must match a keyword hit; only each document's newest version is searched. Built in C7. |

## 4. Components and stack

| Layer | Choice | Fallback / later | Reason |
| --- | --- | --- | --- |
| API | FastAPI, Pydantic v2, Python 3.12 | | Standard AI backend; async |
| Database | PostgreSQL 16 + pgvector | OpenSearch if search outgrows it | One datastore |
| Queue | Procrastinate (Postgres-backed) | Taskiq + SQS | No extra infrastructure; enqueue in the same transaction as the row |
| Born-digital parsing | Docling (MIT) | pdfplumber / pypdfium2 | Permissive licence, good tables |
| OCR for scans | **v1: Docling's built-in OCR.** A hosted adapter (AWS Textract) and self-hosted PaddleOCR-VL come at later checkpoints | Tesseract | Departure from research (which put PaddleOCR-VL first): it needs a GPU and its throughput on our hardware is unmeasured. We add it once the eval set can show whether it earns its cost. |
| LLM | Provider interface. Development default: Gemini API, model `gemini-3.5-flash-lite` (free tier, synthetic data only). Production default chosen per client | Any structured-output model, hosted or local | Client can pick the provider that meets their data-retention terms |
| Extraction library | Pydantic schemas + native structured output, Instructor for retries | BAML | Provenance by schema design |
| Embeddings | Provider interface; default a hosted embedding model | Local model | Swappable for VPC installs |
| Object storage | S3 with Object Lock for originals (MinIO locally) | | Unaltered originals |
| Review UI | Next.js + TypeScript | | Page image with highlighted source box beside the field |
| Auth | API keys for machines; OIDC sessions for people; roles in Postgres | | RBAC + row-level security |
| Observability | OpenTelemetry + Langfuse (self-hosted) | Audit tables only | Traces, cost, datasets |
| PII | Presidio before logging and before LLM calls where configured | regex | Redaction |
| Infra | Docker Compose locally; AWS ECS Fargate + RDS + S3 by Terraform | Kubernetes when a client needs it | Smallest thing that scales horizontally |

**Licences we keep out of the codebase:** PyMuPDF (AGPL), Marker/Surya weights (revenue-limited),
MinerU (custom terms), Dramatiq (LGPL), Phoenix (ELv2), and the FUNSD/SROIE/DocVQA datasets.

## 5. Data model (core tables)

All tenant tables carry `tenant_id` and are protected by row-level security.

| Table | Purpose |
| --- | --- |
| `tenants`, `users`, `roles`, `api_keys` | Identity and access |
| `documents` | One per uploaded file: tenant, type, SHA-256, storage key, status |
| `document_versions` | Each processing run: parser version, schema version, prompt version, model ID |
| `pages`, `blocks` | Parse output: text, block type, bounding box, OCR confidence |
| `extractions` | One per document version; immutable once written |
| `fields` | Field path, value, status (`proposed` / `accepted` / `corrected` / `rejected`), confidence, cited block IDs |
| `field_revisions` | Old value, new value, who, when, reason |
| `validations` | Rule ID, rule version, result, message |
| `matches`, `discrepancies` | Cross-document links and their differences |
| `review_tasks` | Queue for people: reason, assignee, outcome |
| `signatures` | Signer, meaning, time, hash of the record version signed |
| `audit_log` | Append-only, hash-chained: actor (user or model run), action, target, before/after, reason |
| `model_runs` | Provider, model, prompt version, tokens, cost, latency |
| `chunks` | Text, embedding, `tsvector`, block references |
| `webhook_deliveries` | Outbound events with retry state |

As built in C3: verification, rule results and the accept/review decision are stored as one JSON
document per version in `assessments`, and each invoice-to-order comparison as one row in `matches`
holding its discrepancies. Both are immutable. The per-field `fields`, `validations` and
`discrepancies` tables above arrive with review (C5), when a field needs its own status and
history. The decision has two levels with reasons; there is no numeric confidence until there are
reviewer outcomes to calibrate one against.

As built in C6: every request carries an API key or a reviewer session (both stored as hashes);
the tenant comes from it. Postgres row-level security on every tenant table, keyed on a
transaction-local setting, under a role with no DELETE, TRUNCATE, ownership or bypass; migrations
run as the owner. Webhooks from an outbox written with each change, signed and retried with a
fixed event id. The audit chain's head is anchored in object storage.

As built in C5: reviewers (with a scrypt-hashed PIN), corrections and signed reviews are their own
tables, append-only; the record a reviewer sees is the model's reply with the corrections applied,
read again by the normaliser and checks. A signature stores the SHA-256 of the record and of who,
what, why and when; the database refuses corrections to a signed version and approvals over open
checks without an override reason. The review screen is a Next.js app calling the API from the
browser.

As built in C4: the parser runs in a child process started fresh (not forked), replaced after
50 documents and stopped if one document exceeds a time or memory limit; replies cross the pipe
as JSON, and the child gets no credentials. A file whose every page has a text layer is read from
it; otherwise every page is rendered at 200 dpi, straightened, and read by OCR (RapidOCR through
Docling), and block boxes are mapped back onto the page as uploaded. Documents of more than one
page are sent to the model one page per request and merged in code; a field that two pages give
differently is flagged for review.

`audit_log` is append-only and hash-chained per tenant. As built in C2: database triggers reject
UPDATE, DELETE and TRUNCATE, each entry may have only one successor, and each entry stores the hash
of the one before it, so an entry edited or removed from the middle is evident when the chain is
recomputed. Not yet built: a restricted application role without rights to change the table (C6),
and a copy of the latest hash kept outside the database. Until both exist, someone with owner
access to the database could rewrite the log and recompute its hashes, so we describe the log as
tamper-evident against ordinary access, not tamper-proof.

## 6. Reliability and scale

| Risk (from research) | Design answer |
| --- | --- |
| Valid JSON, wrong values | Block-citation check, deterministic rules, cross-document match, 1–2% audit sample, alerts on rejection-rate and volume shifts |
| Vision model misreads digits | Numbers must equal the OCR text of their cited blocks |
| Table errors across pages | Table-aware parser, header carry-over across pages, line-arithmetic checks |
| Parser memory leaks | Subprocess workers recycled per N documents, page streaming, page cap |
| Duplicates and retry storms | Content-hash idempotency, jittered backoff honouring Retry-After, permanent vs transient error classes, dead-letter state |
| pgvector recall with tenant filter | Tenant-partitioned or partial HNSW indexes, iterative scan, measured recall in evals |
| Prompt injection in documents | Document text is passed as data; text layer compared to OCR; no tool access in the extraction call |
| Cost blow-ups | Cheap path for simple pages, batch API for bulk, caching by content hash, per-tenant cost caps |
| Prompt or model regressions | Pinned model IDs, versioned prompts, eval gate in CI before any change ships |

**Scaling path.** API and workers are stateless and scale horizontally. Stage one: one API task, two
worker pools (parse, extract), one Postgres. Stage two: read replica, per-stage worker autoscaling on
queue depth, self-hosted OCR on GPU. Stage three (only if measured need): move queue to SQS and search
to OpenSearch. Each move is a swap behind an existing interface.

**Speed targets (to be measured at the load-test checkpoint; they are goals, not results).**
Born-digital 3-page invoice end to end p95 under 15 s; scanned under 40 s; API reads p95 under 200 ms;
search p95 under 300 ms. Upload returns immediately with a job ID.

## 7. Compliance-supporting features

Mapped from the checklist in `04-pharma-compliance.md`.

| Requirement | Where it lives |
| --- | --- |
| Attributable, time-stamped, append-only audit trail with reason | `audit_log`, `field_revisions` |
| Original records preserved unaltered | SHA-256 + S3 Object Lock |
| Versioned records with diffs | `document_versions`, `extractions`, `field_revisions` |
| Field-level provenance | `fields.block_ids` → `blocks.bbox` |
| Unique users, roles, tenant isolation | OIDC, RBAC, row-level security |
| E-signature bound to a record version | `signatures` with re-authentication |
| Human acceptance of AI output | `proposed` → `accepted` state machine, review UI |
| Model and prompt traceability, change control | `model_runs`, pinned versions, eval gate |
| Retention, legal hold, deletion with tombstone | Retention policy per tenant |
| Export of record plus audit trail | Export endpoint (JSON, CSV, PDF report) |
| Validation-support documents | `docs/validation/`: intended use, requirements, risk assessment, test evidence, traceability |

Open items to confirm against primary texts before any public claim: Annex 11 clause numbers, the
status of the Annex 11 revision and Annex 22, Schedule M wording, GST rule citations, and the current
retention terms of whichever model provider a client uses.

## 8. Interfaces for later projects

| Consumer | Interface |
| --- | --- |
| MindAtlas 2.0 | Read API for blocks, chunks and citations; search endpoint |
| AgentOps | Webhooks (`document.accepted`, `discrepancy.raised`, `review.completed`); idempotent command endpoints |
| RelayDesk | An MCP server over the same read API with scoped tokens and audit entries |

## 9. Evaluation

- **Golden set:** synthetic pharma invoices, POs and CoAs generated from templates with known ground
  truth, in clean, scanned, skewed and low-resolution variants, plus about 50 hand-checked documents.
  No real company data.
- **Public benchmarks:** DailyMed/openFDA labels, CORD-v2, DUDE.
- **Metrics:** field-level precision and recall by field class, line-item table accuracy, share of
  documents accepted without review, false-accept rate, cost and latency per document.
- **Gate:** CI replays recorded model responses offline for determinism; a scheduled live run tracks drift.
  A change that lowers accuracy on a critical field class does not merge.
