# TDD evidence: C2 async and durable

Source plan: checkpoint C2 in `docs/build-plan.md`; journeys derived during this run.

## Journeys

1. As an integrator, I upload a file and get an answer at once; the extraction arrives later and I
   can ask for its status.
2. As an integrator, I can upload the same file again, by mistake or on retry, without creating a
   second document or paying for a second extraction.
3. As an operator, I can lose a worker at any moment and the document is still extracted, once.
4. As a reviewer of the record, I can see every step taken on a document and check that the log has
   not been edited.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| Schema (tests and migration together) | `712a8a3` | 28 integration tests pass; RED was the import error before the migration existed |
| RED storage, audit, service | `1db6a8f` | collection fails: `docforge.storage`, `docforge.audit`, `docforge.documents` missing |
| GREEN storage, audit, service | `c7bf649` | 79 integration tests pass on Postgres 16 and MinIO |
| RED queue, worker, API | `b5e6a3b` | collection fails: `docforge.queue`, `docforge.worker` missing |
| GREEN queue, worker, API | `bc70056` | 878 tests pass, including the test that kills a real worker process |
| RED review fixes | `cdd7a6d` | collection fails: `DocumentTypeConflict`, `QueueFull`, `ReprocessInProgress` missing |
| GREEN review fixes | `988369f` | 907 tests pass (766 unit, 115 integration, 26 Docling) |

The schema slice did not get a separate RED commit: its tests and migration were written together.

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | The same file twice is one document, one job and one stored object | `test_documents.py`, `test_queue.py`, `test_api_documents.py` | integration |
| 2 | The job is created in the same transaction as the document: if either fails, neither exists | `test_queue.py::test_the_job_is_rolled_back_with_the_rest_of_the_ingest`, `test_documents.py::test_if_the_job_cannot_be_queued_no_document_is_recorded` | integration |
| 3 | A real worker process killed with SIGKILL mid-job loses nothing: a second worker finishes the job and one extraction exists | `test_worker_crash.py` | integration, real processes |
| 4 | A delivery that was overtaken (late failure or late success) writes nothing | `test_documents.py::test_a_late_failure_cannot_undo_...`, `::test_a_late_success_does_not_write_a_second_extraction` | integration |
| 5 | A document that keeps killing its worker is failed after the attempt limit, without running again | `test_documents.py::test_a_document_that_keeps_killing_its_worker_...` | integration |
| 6 | Provider failures are retried by the queue and the version is failed on the last attempt | `test_queue.py`, `test_documents.py` | integration |
| 7 | A job whose worker stopped heart-beating is requeued; a job with a live worker is not | `test_queue.py` | integration |
| 8 | Any unexpected error in the task leaves the job on the queue | `test_queue.py::test_a_database_error_in_the_task_is_retried_not_dropped` | integration |
| 9 | The stored original is checked against its recorded hash before extraction | `test_documents.py::test_a_changed_original_is_not_extracted` | integration |
| 10 | An edited, removed or forged audit entry breaks the chain at a reported entry | `test_audit.py` | integration |
| 11 | The database itself refuses a second successor or a second first entry, and refuses UPDATE, DELETE and TRUNCATE on the audit log and on extractions | `test_audit.py`, `test_schema_c2.py` | integration |
| 12 | Eight concurrent writers form one unbroken chain with timestamps in order; eight documents processed at once all finish | `test_audit.py`, `test_documents.py` | integration |
| 13 | Reprocess adds a version and keeps earlier extractions; it is refused while a version is in flight | `test_documents.py`, `test_api_documents.py` | integration |
| 14 | Another tenant cannot read or reprocess a document | `test_documents.py::test_another_tenant_cannot_read_or_reprocess_a_document` | integration |
| 15 | Migration 0002 upgrades a database that already has rows, and refuses to discard audit records on downgrade | `test_schema_c2.py` | integration |
| 16 | The audit hash is reproducible from an entry's stored fields | `tests/unit/test_audit_hash.py` | unit |

All rows: PASS under `make test` locally and in CI.

## Coverage and gaps

`make test` reports 96% line and branch coverage. The worker process started by the crash test is
not measured, so `worker.py` and `wiring.py` show lower figures than they are exercised.

Not covered by tests:

- A real worker process killed at the exact point after the model call and before the write. A
  test covers that window inside the service; the process-level kill checks land during the call.
- The deadlock the database review described was later shown not to exist (see `progress.md`,
  second verification pass). A test now runs reprocess against the worker on one document.
- The retry wait times: tests run with a wait of zero.
- Two workers racing to requeue the same stalled job.
