# TDD evidence: C15 Claude and OpenAI as model providers

Source plan: `docs/plans/c15-providers.md` (ECC planner). Owner: the roadmap item is needed
(2026-10-06); which providers and models, their keys and budget are still to come.

## Journeys

1. As the operator, I choose who reads documents and who answers questions (Gemini, Claude or
   OpenAI), each pinned to a model, and nothing changes if I choose nothing.
2. Before switching a provider on, I see it measured on the same eval sets against the same
   floors as Gemini, side by side with its cost and how sure each number is.
3. As a customer's data officer, I know where each kind of text goes, that OpenAI keeps
   nothing it is sent, and that keys never appear in logs or errors.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED groundwork | `9c4dbe5` | strict schema, recording keys, provider settings: 12 failing |
| GREEN | `4a1568d` | 46 unit tests |
| RED providers | `e6a0221` | provider tests fail to import |
| GREEN | `ab0a79b` | 9 provider tests through the real SDKs over a fake transport |
| RED cost | `58a7382` | 3 cost tests, 1 chat test |
| GREEN | `e4f807a` | migration 0023 |
| RED wiring | `f4aba74` | |
| GREEN | `591f5da` | |
| RED evals | `6a88e54` | |
| GREEN | `c9190f1` | every Gemini report replays unchanged |
| RED replay deployment | `6a7b738` | the demo would have refused to start |
| GREEN | `cd2ac06` | |
| RED review findings (round 1) | `9ed7840` | 20 failing |
| GREEN | `3f89b70` | 1,406 unit tests |
| RED review findings (round 2) | `8c7125c` | 7 failing, compare missing |
| GREEN | (with this record) | 1,918 tests; gate 76/76; every Gemini report replays (one new field) |

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | The reply schemas are strict: self-contained, closed, every field present; field names that are schema words kept; a single value an enum; recursion and unsupported keywords refused | `tests/unit/test_llm_schema.py` | unit |
| 2 | Gemini's recordings keep their keys; another provider's are keyed by provider, its request shape and the strict schema, in a directory of its own | `tests/unit/test_recording_keys.py` | unit |
| 3 | A provider and pinned model per task; `-latest` refused; production needs each selected provider's key and Gemini's for search, except a replay deployment; prices real and non-negative; a bounded timeout | `tests/unit/test_config.py` | unit |
| 4 | Claude and OpenAI through their SDKs: what is sent, tokens, retries as the server asks (`retry-after-ms` first), one time budget per call, out of credit a quota stop, refusals and bad requests not retried, the served model recorded | `tests/unit/test_llm_providers.py`, `test_llm_retrying.py` | unit |
| 5 | The extraction pipeline reads an invoice through each provider, a malformed reply asked for again | `tests/unit/test_pipeline_providers.py` | unit |
| 6 | Each call priced by its own provider and model; none shown if any is unpriced | `tests/unit/test_telemetry.py` | unit |
| 7 | An answer records the provider that answered (migration 0023) | `tests/integration/test_chat.py` | integration |
| 8 | Each task's provider built from the settings; a replay deployment never calls one | `tests/unit/test_wiring_providers.py` | unit |
| 9 | Evals against another provider: reports kept apart, its key needed to record, moving aliases refused, suites without a model call refusing it | `tests/unit/test_eval_providers.py` | unit |
| 10 | Every report from a model says whose and pins provider, model and prompts; a provider with any report is held to every Gemini floor, a missing report failing | `tests/unit/test_eval_gate.py` | unit |
| 11 | Providers side by side with 95% intervals and cost in dollars; what is not measured says so | `tests/unit/test_eval_compare.py` | unit |
