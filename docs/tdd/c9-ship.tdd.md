# TDD evidence: C9 ship

Source plan: checkpoint C9 in `docs/build-plan.md`; journeys derived during this run.

## Journeys

1. As a prospective client, I open the demo from a clean browser, sign in with the account
   shown, and review, search and sign on synthetic documents.
2. As the owner, I deploy, redeploy, rotate secrets and turn the demo off from a runbook,
   with no long-lived keys and a budget alert.
3. As a visitor with bad intent, I cannot lock others out, reach the database owner, fill the
   disk or relay spam.
4. As a validating organisation, I get intended use, traceability to tests and a mapping to
   the regulations, gaps included.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED S3 on AWS | `63edc2d` | 2 failed |
| GREEN | `954b97e` | unit suite passes |
| RED demo seed | `e2ad6f0` | module missing |
| GREEN, images | `187f828` | 3 passed; images build, no secrets in them |
| RED hybrid fallback | `7d582c3` | EmbeddingMissing raised |
| GREEN | `0374005` | 27 search tests pass |
| RED demo sign-in hint | `f55a49e` | module missing |
| GREEN, deploy stack | `812cb99` | web tests pass; e2e 7 passed |
| Host scripts; Terraform | `cf5064b`, `2b59f94` | local stack trial; terraform validate, checkov, tflint |
| RED Python review | `edc0804` | 3 failed |
| GREEN | `af264e1` | 53 passed |
| RED security and React review | `7f8b984` | 6 failed (3 Python, 3 web) |
| GREEN | `b9badec` | the affected suites pass; e2e 7 passed |
| Deploy and infrastructure review fixes | `aa230bd` | local stack trial: no owner credentials in the API container; 6 wrong PINs then sign-in 201; upload 403 for the demo account; 13 MB body 413; reset and ops check work; checkov 127/0/19 |

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | On AWS the store uses the default credential chain in its region; MinIO still uses its keys | `tests/unit/test_storage_aws.py`, `test_config.py` | unit |
| 2 | The demo seed adds an organisation, a shared reviewer (not an admin) and the documents, once | `tests/integration/test_demo.py` | integration |
| 3 | A shared account is never locked, at sign-in or at signing | `test_auth.py`, `test_review.py` | integration |
| 4 | Hybrid search answers from words alone when it must, says so, and pauses a failed service | `tests/integration/test_search.py` | integration |
| 5 | Only a quota means "busy"; a rejected key is an error | `tests/unit/test_embeddings.py` | unit |
| 6 | The sign-in hint shows only on the demo; the client address passes only through a trusted proxy; large bodies are refused | `web/tests/unit/demo.test.ts`, `server.test.ts` | unit |
| 7 | The production stack runs: HTTPS, seed, review, search, reset, ops check | local trial (`.deploytest/`, described above) | manual, recorded |
| 8 | The infrastructure is valid and passes the security scan | `terraform validate`, checkov, tflint (in containers) | static |

## Coverage and known gaps

`make test` 1,560 tests, 92%. Not tested: anything on AWS itself (see Honest limits under C9
in `docs/progress.md`).
