# TDD evidence: C0 Foundations

Source plan: checkpoint C0 in `docs/build-plan.md`; journeys derived during this run.

## Journeys

1. As a developer, I run `make up` and `make test` on a fresh clone and both pass.
2. As a developer, I apply and roll back the schema with one command and trust its constraints.
3. As the person building evals, I have 20 invoice/PO pairs whose labels are exactly what the
   PDFs say, with the position of each value.
4. As a maintainer, I regenerate the fixtures and get the same bytes.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED 1 | `793973b` | `uv run pytest`: collection fails, `ModuleNotFoundError: docforge.config` |
| GREEN 1 | `0c19211` | `make test`: 17 passed, coverage 97%; `make lint` clean |
| RED 2 | `75e5e8e` | `uv run pytest tests/unit`: collection fails, `docforge.gstin` and `docforge.synth` missing |
| GREEN 2 | `5173475` | `make test`: 339 passed, coverage 99%; `make lint` clean |
| RED 3 (review fixes) | `1f363c0` | collection fails: `leaf_paths`, `line_amounts`, `compute_totals` missing |
| GREEN 3 | `b9591d8` | `make test`: 575 passed, coverage 99%; CI green on `ubuntu-24.04` |

One assertion was wrong when first written: the anonymous bucket check passed for a missing bucket
too (MinIO returns 403 for both). It was replaced with a signed `head_bucket` call before GREEN 1.

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | Settings default to the Compose services; environment overrides them | `tests/unit/test_config.py` | unit |
| 2 | Secrets, including the database password, never appear in `repr` or validation errors | `tests/unit/test_config.py` | unit |
| 3 | Only `postgresql+psycopg` URLs are accepted; production refuses local defaults | `tests/unit/test_config.py` | unit |
| 4 | Migration creates the core tables and pgvector, and survives down then up | `tests/integration/test_migrations.py` | integration |
| 5 | A content hash is unique per tenant, allowed across tenants, and must be 64 hex characters | `tests/integration/test_migrations.py` | integration |
| 6 | Postgres is version 16; the object store is live; the originals bucket exists | `tests/integration/test_services.py` | integration |
| 7 | GSTIN check character matches published samples; malformed and altered GSTINs fail | `tests/unit/test_gstin.py` | unit |
| 8 | Line and total arithmetic match hand-computed values, including a half-paisa tie and a half-rupee round-off | `tests/unit/test_synth_builder.py::TestHandComputedArithmetic` | unit |
| 9 | Every pair is internally consistent: arithmetic, GSTINs, supply type, dates, batches, PO agreement, scheme quantities | `tests/unit/test_synth_builder.py::TestEveryPair` | unit |
| 10 | Every box lies on the page, overlaps no other box, and the PDF draws that text at that position | `tests/unit/test_synth_render.py::TestEveryRenderedDocument` | unit |
| 11 | Every label value is either boxed or listed as unprinted | `tests/unit/test_synth_render.py`, `tests/unit/test_synth_dataset.py` | unit |
| 12 | A failed generation leaves the existing set untouched; stale pairs are removed; unrelated files are kept | `tests/unit/test_synth_dataset.py` | unit |
| 13 | The 20 committed pairs equal a fresh generation byte for byte | `tests/unit/test_synth_dataset.py` | unit |

All rows: PASS under `make test` locally and in CI.

## Coverage and gaps

`make test` reports 99% line and branch coverage. Uncovered: the `if __name__ == "__main__"` line
and two branch arcs (`db.alembic_config` without a URL, a batch-number collision retry).

Not tested: the box check confirms a text run starts inside its box, not that the box's width and
height are exact. Unboxed text (headers, labels) is not checked for overlap.
