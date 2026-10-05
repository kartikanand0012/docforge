# Portfolio card

For an Upwork portfolio entry or a project summary. The numbers are from `docs/progress.md`;
keep them in step if they change.

**Title:** DocForge: document intelligence with an audit trail, built for regulated operations

**One line:** PDFs and scans become checked records. Every value links to its place on the
page, and a model's output becomes a record only after rules or a person accept it.

**The problem it solves:** pharma distributors and other document-heavy businesses re-key
invoices, purchase orders and certificates of analysis by hand. A generic OCR or LLM extractor
is fast but cannot be trusted with a batch number or a price. DocForge reads the documents,
proves each value against its source, checks it with rules and against related documents, and
sends only what needs a person to a review screen with an e-signature.

**What it does**
- Extract and act: an approved invoice becomes a payment approval draft, delivered by signed
  webhook to the client's own systems.
- Per-field checks with a review queue for anything not proven, and the reason for each.
- A public eval page: accuracy per document type on labelled documents, with a CI gate that
  blocks a worse prompt or model.
- Cost and latency shown in the open: $0.0135 per one-page invoice; p95 latency measured
  under load.

**Measured** (synthetic documents)
- **Scans:** 98.1% of values read correctly on poor scans.
- **Defects:** 15 of 15 seeded defects caught.
- **Search:** 88% of held-out questions find the right document in the top five, and no
  result ever comes from another organisation.

**Built with:** Python, FastAPI, PostgreSQL (row-level security, pgvector, full text), Docling
and OCR, Gemini with structured output, Next.js, OpenTelemetry, Terraform on AWS. Over 1,500
automated tests; every eval is replayed offline in CI.

**Designed to support** 21 CFR Part 11 and EU GMP Annex 11 programmes:
- an append-only, hash-chained audit trail;
- e-signatures bound to the exact record signed;
- a validation-support pack.

It is not certified; the customer validates it for their own use.

**Links:** live demo (synthetic data) · source · 90-second walkthrough
