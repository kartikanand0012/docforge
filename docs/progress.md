# DocForge progress

One entry per checkpoint: what passed, the measured numbers, and what changed from the plan.

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
