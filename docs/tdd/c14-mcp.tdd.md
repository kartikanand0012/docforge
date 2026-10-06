# TDD evidence: C14 MCP server for AI agents

Source plan: `docs/plans/c14-mcp.md` (ECC planner); roadmap `docs/research/08-feature-roadmap.md`
section 8 ("MCP server, read-only, per API key"). Owner's decisions: a new read-only `reader`
role and only such keys at `/v1/mcp`; only administrators make them; no Claude Desktop bridge.

## Journeys

1. As an administrator, I make a read-only key for an AI agent, copy one command into Claude
   Code, see what the agent calls, and revoke the key when it is no longer needed.
2. As an AI agent with that key, I list knowledge bases and documents, search, ask for checked
   and quoted answers, and read a document's fields or a page - only my organisation's.
3. As the organisation, I know an agent's key can never upload, review or change anything,
   that text in a document cannot pass as instructions, and that every call is recorded
   without its text.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED | `a607cd5` | `tests/integration/test_mcp.py` and `tests/unit/test_injection.py` fail to import (`docforge.mcp_server`, `docforge.chat.injection`) |
| GREEN | `65d2290` | 16 MCP and injection tests pass; web 75; mcp eval 13 measures; gate 60/60 |
| RED review findings | `a3a19f8` | 3 unit and 5 integration tests fail (also carries the page's rework for the React review and this record) |
| GREEN | `b464626` | 1,842 Python tests; web 75; mcp eval 13/13 with the hardened measures; gate 60/60 |

The web helpers' tests (`web/tests/unit/agents.test.ts`) were run failing before the page
existed, then committed with it.

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | An agent starts with the data-not-instructions notice and finds six tools, all read-only and closed-world | `test_an_agent_starts_with_the_notice_and_finds_six_read_only_tools` | integration |
| 2 | No key, a made-up or revoked key: 401 with `WWW-Authenticate: Bearer`; integrator and admin keys and a person's session: 403 | `test_without_a_reader_key_nothing_is_answered` | integration |
| 3 | A foreign Origin 403, a body over 64 KB 413, GET 405 | `test_a_foreign_origin_an_oversized_body_and_get_are_refused` | integration |
| 4 | Search returns the organisation's passages with document, page and the notice | `test_search_finds_the_organisations_passages_marked_as_data` | integration |
| 5 | `ask` gives the checked answer with its quotes; a follow-up keeps its conversation | `test_ask_gives_the_checked_cited_answer_and_a_follow_up_keeps_its_scope` | integration |
| 6 | An unanswerable question comes back with its reason and what was missing, not as an error | `test_an_unanswerable_question_comes_back_with_its_reason_not_as_an_error` | integration |
| 7 | Knowledge bases, documents (paged), a document's fields and a page's text are read; a page that does not exist is "Not found." | `test_documents_and_knowledge_bases_are_listed_and_read` | integration |
| 8 | Another organisation's document, knowledge base and conversation ids are "Not found." in every tool; its text never seen | `test_another_organisations_ids_are_not_found_and_its_text_never_seen` | integration |
| 9 | Two keys at once each see only their own organisation | `test_two_keys_at_once_each_see_their_own_organisation` | integration |
| 10 | Text that reads like instructions is withheld from page text and search, and flagged | `test_text_that_reads_like_instructions_is_withheld` | integration |
| 11 | Calls are limited per key per minute | `test_calls_are_limited_per_key` | integration |
| 12 | Bad arguments are refused | `test_bad_arguments_are_refused` | integration |
| 13 | Every call is recorded (tool, outcome, key), never its query or text | `test_every_call_is_recorded_without_its_text` | integration |
| 14 | Administrators make (token once), list and revoke reader keys, audited in the hash chain; reviewers cannot | `test_an_administrator_makes_lists_and_revokes_a_read_only_key` | integration |
| 15 | Hidden characters removed and compatibility forms folded; instructions recognised, ordinary text not | `tests/unit/test_injection.py` | unit |
| 16 | The endpoint, the Claude Code command, outcomes in words | `web/tests/unit/agents.test.ts` | unit |
| 17 | An administrator makes a key on the page, the key works against the API, and after Revoke it is refused | `web/tests/e2e/review.spec.ts` (last test) | e2e |
| 18 | Six read-only tools, every call succeeds, `ask` equals `/v1/chat`, 0 cross-tenant leaks, 0 wrong keys accepted, 0 planted instructions passed, 0 oversized results, limits hold, every call recorded and none with text | `evals/gate.json` over `evals/baselines/mcp.json` | eval |
