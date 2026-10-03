# Requirements and traceability

Each requirement, where the system meets it, and the automated tests that show it. Tests run
on every change in CI (`make test`); accuracy requirements are measured by the evals (`make
eval`) and held by the gate (`make gate`). Paths are relative to the repository root.

| ID | Requirement | Where it is met | Shown by |
| --- | --- | --- | --- |
| R1 | The original file is kept unchanged and identified by its content hash | `documents.py` (ingest), `storage.py` | `tests/integration/test_documents.py`, `test_storage.py` |
| R2 | The same file uploaded twice is one document; reprocessing makes a new version, never overwrites | `documents.py` | `test_documents.py`, `test_schema_c2.py` |
| R3 | Every extracted value cites the blocks on the page it was read from | `extraction/pipeline.py`, `extraction/normalize.py` | `tests/unit/test_pipeline.py`, `test_normalize.py`; eval `citations.accuracy` |
| R4 | A value that its cited text does not support is flagged, never silently corrected | `trust/verify.py` | `test_trust_verify.py`, `test_verify_contains_equivalence.py` |
| R5 | Deterministic rules check identifiers, dates, arithmetic and prices | `trust/rules.py`, `trust/invoice_rules.py`, `gstin.py` | `test_trust_rules.py`, `test_gstin.py` |
| R6 | An invoice is compared with its purchase order; differences are listed with severity | `trust/match.py` | `test_trust_match.py`, `test_assessment.py` |
| R7 | A certificate's results are checked against their limits; a limit or conclusion not fully understood is "not evaluated", never passed | `trust/limits.py`, `extraction/coa.py` | `test_coa_limits.py`, `test_coa_spec.py` |
| R8 | An invoice whose batch has an out-of-limit or unverifiable certificate is held for a person | `review/service.py` | `test_coa_link.py` |
| R9 | A document is accepted only when nothing needs a person; otherwise it goes to review with reasons | `trust/assess.py` | `test_trust_assess.py`, `test_review.py` |
| R10 | A correction keeps the old value, the new value, the reason and the person | `review/service.py`, `review/revise.py` | `test_review.py`, `test_review_logic.py` |
| R11 | A signature names the person, the time and a fixed meaning, needs the PIN again, and covers a hash of the exact record signed | `review/signing.py`, `review/service.py` | `test_review.py`, `test_api_review.py` |
| R12 | A record changed after it was shown cannot be signed as shown | `review/service.py` (`expected_record_sha256`) | `test_review.py` |
| R13 | Every action is in an append-only, hash-chained audit log; a rewritten log is detected, even with recomputed hashes | `audit.py`, `anchors.py`, migration triggers | `test_audit.py`, `test_audit_hash.py`, `test_exports_and_anchors.py` |
| R14 | Each organisation sees and changes only its own data, enforced by the database | `db/tenancy.py`, migrations 0008, 0010 | `test_rls.py`, `test_privileges.py`, `test_auth.py` |
| R15 | Only authenticated callers act, within their role; credentials are stored only as hashes; repeated wrong PINs lock the account | `auth.py`, `api/auth.py` | `test_auth.py`, `test_auth_tokens.py` |
| R16 | Signed records can be exported as CSV (formula-safe) and JSON | `api/exports.py` | `test_exports_and_anchors.py` |
| R17 | Other systems are told of results by signed webhooks, retried, never delivered twice | `webhooks.py` | `test_webhooks.py`, `test_webhook_rules.py` |
| R18 | A model provider failure or a malformed reply never produces a record | `extraction/pipeline.py`, `documents.py` | `test_pipeline.py`, `test_documents.py`, `test_worker_crash.py` |
| R19 | Accuracy does not fall below the measured floors when the prompt, model or code changes | `evals/gate.py`, `evals/gate.json` | `test_eval_gate.py`, `test_eval_gate_demo.py`; CI gate |
| R20 | Traces and operational reports carry no document content | `telemetry.py`, `ops.py` | `test_tracing.py`, `test_ops.py` |

## Measured performance (from `docs/progress.md`)

These are measured on synthetic documents and are a starting point for the organisation's own
performance qualification on its own documents, not a substitute for it.

| Measure | Value | Source |
| --- | --- | --- |
| Printed fields correct, clean synthetic invoices | 2,472 of 2,472 | `evals/baselines/invoice.json` |
| Printed fields correct, poor scans | 98.06% | `evals/baselines/scans.json` |
| Seeded invoice defects caught | 9 of 9 | `evals/baselines/trust.json` |
| Out-of-limit certificate results caught | 6 of 6 | `evals/baselines/coa.json` |
