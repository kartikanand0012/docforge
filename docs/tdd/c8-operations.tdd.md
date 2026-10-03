# TDD evidence: C8 operations

Source plan: checkpoint C8 in `docs/build-plan.md`; journeys derived during this run.

## Journeys

1. As the owner, a change that makes extraction or search worse cannot merge unnoticed.
2. As an operator, I can see where a document's time and money went, without the trace store
   holding any document content.
3. As an operator, I am told when work stops moving or starts failing.
4. As the owner, I know the system's latency and cost from measurement, and where it breaks.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED eval gate, cost with thinking tokens | `6ce4108` | collection fails; cost test fails |
| GREEN | `a411ce4` | 20 of 20 checks pass |
| RED gate proof | `fc2a500` | collection fails |
| GREEN (two prompts recorded live) | `bca80b9` | normalising prompt blocked; shortened passes |
| RED tracing; RED deterministic ties | `159306f`, `cb2c047` | module missing; order differs |
| GREEN | `79353c9` | tracing tests pass; search eval identical over 6 runs |
| RED alerts | `1c96b0e` | module missing |
| GREEN | `afd967a` | 7 passed |
| RED load runner, vector scale | `a3a596c` | modules missing |
| GREEN; RED per-row subquery and backfill | `11510e7` | 2 failed |
| GREEN indexed version | `4ffa68d` | p95 195 -> 21 ms at 50,000 chunks |
| Review queue performance (guarded by an equivalence test) | `b77d36c` | same answers on 40,000 cases |
| RED held-out search | `c4bad36` | module missing |
| GREEN (recorded live) | `e7dc26c` | hybrid recall@5 0.88 |
| RED security review | `64cc7f9` | question and reply text on spans |
| GREEN | `9e24d54` | tracing tests pass |
| RED review findings | `1e86245` | 16 failed |
| GREEN | `44feaf5` | `make test` 1,547 passed; `make eval` unchanged; gate 35 of 35; e2e 7 passed |

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | A worse report fails naming its metric; missing reports, metrics and non-numbers fail; dataset, model and prompt are pinned | `tests/unit/test_eval_gate.py` | unit |
| 2 | A metric worse than the base branch fails even above its floor; a loosened or removed floor is named | `tests/unit/test_eval_gate.py` | unit |
| 3 | A prompt that tidies values is blocked; a shortened prompt passes every quality floor | `tests/unit/test_eval_gate_demo.py` | unit (replayed live recordings) |
| 4 | Cost counts thinking tokens as output; no price, no cost | `test_eval_cost.py`, `test_telemetry.py` | unit |
| 5 | Each stage is a span under one trace; model calls carry tokens, documents cost | `tests/integration/test_tracing.py` | integration |
| 6 | No document text, question or error message reaches a span, on success or failure | `tests/integration/test_tracing.py` | integration |
| 7 | Alerts for no worker, a stalled queue, a backlog, failed jobs, failing documents; unreachable is critical; counts only | `tests/integration/test_ops.py` | integration |
| 8 | The load run reports every phase, fails on any failure, survives a lost poll | `tests/unit/test_load.py` | unit |
| 9 | Vector search at scale: no per-row subquery, recall against exact, nothing across tenants | `tests/integration/test_vector_scale.py` | integration |
| 10 | Only the newest indexed version is searched; the pointer repairs itself and stays on its document; equal scores order the same every time | `tests/integration/test_search.py` | integration |
| 11 | The held-out search report is reproduced offline | `tests/integration/test_eval_search_heldout.py` | integration |
| 12 | Whole-token matching answers as the regex did | `tests/unit/test_verify_contains_equivalence.py` | unit |

## Coverage and known gaps

`make test` 1,547 passed, 93%. Gaps are listed under C8 in `docs/progress.md`.
