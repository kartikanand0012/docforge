# DocForge build plan (v0.1, for sign-off)

2026-10-01. Companion to `architecture.md`. Nothing here has been started.

## How each checkpoint runs

Every checkpoint is a working end-to-end slice. We do not start the next one until the gate passes.

1. **Plan:** write the checkpoint's short spec and acceptance tests (ECC planning workflow).
2. **Test first:** failing tests for the acceptance criteria (ECC test-driven workflow).
3. **Build:** implement until tests pass.
4. **Review:** independent code review and security review of the diff (ECC review agents).
5. **Gate:** full test suite, eval run, and a manual end-to-end check through the real API or UI.
6. **Record:** update `docs/progress.md` (what passed, measured numbers) and
   `docs/research/05-interview-topics.md` (what you learned, tagged to the code).

ECC must be loaded in the session (it is installed but only activates in a new session). The exact ECC
command names get filled in once it is running.

## Checkpoints

| # | Slice | Gate (must be true to move on) | Interview topics it covers |
| --- | --- | --- | --- |
| C0 | Foundations: repo, CI, Docker Compose (Postgres + pgvector, MinIO), migrations, synthetic document generator with ground truth | `make up` and `make test` pass on a clean machine; 20 synthetic invoice/PO pairs with labels exist | Project structure, testing strategy, data for evals |
| C1 | Walking skeleton: born-digital invoice in, schema-validated JSON out, through the API (synchronous) | Eval script runs on the 20 documents and records a baseline accuracy number | End-to-end pipeline design, structured output reliability |
| C2 | Async and durable: job queue, object storage, content-hash idempotency, document versions, audit log | Same file uploaded twice yields one document; killing a worker mid-job loses nothing; audit chain verifies | Queues, idempotency, retries, exactly-once vs at-least-once |
| C3 | Trust layer: block-level provenance, numeric verification, deterministic validators, field confidence, invoice ↔ PO match | Every field links to a page box; seeded errors (wrong batch, bad arithmetic, expired stock) are all caught | Hallucination control, validation, confidence and routing |
| C4 | Scans and tables: OCR path, multi-page tables, skewed and low-resolution inputs, parser worker isolation | Accuracy on the scanned variants is measured and reported next to the clean baseline; a 300-page file does not exhaust memory | OCR vs vision-model trade-offs, table extraction, memory and backpressure |
| C5 | Review: Next.js review screen with source highlighting, accept/correct with reason, e-signature, versioned extractions | A reviewer can resolve a flagged document end to end; every action appears in the audit log | Human-in-the-loop design, state machines, audit trails |
| C6 | Multi-tenant product surface: tenants, roles, row-level security, API keys, webhooks, CSV/JSON export | Cross-tenant access tests all fail closed; webhook retries are idempotent | Multi-tenancy, RLS, API design, security |
| C7 | Search and CoA: chunking, embeddings, hybrid search with citations; CoA vs specification workflow matched to the invoice | Retrieval recall measured on a labelled question set, with and without tenant filter; out-of-limit CoA results are flagged | Chunking, hybrid search, pgvector tuning, debugging a wrong answer |
| C8 | Operations: eval gate in CI with offline replay, tracing, cost per document, alerts, load test | CI blocks a deliberately worse prompt; load test produces real p95 latency and cost numbers | Evals and ship gates, observability, cost and latency optimisation, regression handling |
| C9 | Ship: Terraform to AWS, public demo with synthetic data, README with architecture diagram, 90-second Loom, validation-support docs, portfolio card | Demo URL works from a clean browser; the three published metrics come from C8 measurements | Deployment, infrastructure as code, communicating trade-offs |

## Scope and time

This is more than the 10–14 days the original brief estimated; the research added the trust layer,
review workflow and compliance-supporting features, which are what buyers pay for.

- **Demo-able cut:** C0–C5 then C9. Enough for the Upwork portfolio and the first Catalog offer.
- **Full build:** all ten checkpoints.

I have no measured basis for a day count yet. After C1 we will know the real pace and I will give an
estimate for the rest.

## Decisions (agreed 2026-10-02)

1. Demo scope: distributor invoice + PO match first, CoA check second.
2. OCR: Docling OCR first, Textract adapter and self-hosted PaddleOCR-VL later.
3. Model provider for development: Gemini API free tier, synthetic data only. No AWS account until C9;
   hosting for the demo is decided then.
4. Repo: `docforge`, MIT, private until launch. Internal notes (research, this plan, interview topics)
   must be removed from history before it goes public.
5. Build order: demo-able cut first (C0–C5, then C9), then C6–C8.
6. Commit and push after every passing step.
