# TDD evidence: C12 knowledge bases

Source plan: C12 in `docs/research/06-knowledge-chat-connectors.md`; roadmap
`docs/research/08-feature-roadmap.md` (knowledge bases, streamed answers, citation outlines on
every page).

## Journeys

1. As an organisation, I group documents (procedures, contracts, one supplier's papers) into a
   knowledge base, change it as documents come and go, and delete it without losing them.
2. As anyone in the organisation, I ask within a knowledge base and get answers only from its
   documents, with their quotes.
3. While an answer is being prepared, I see what is happening: searching, reading, checking.
4. A conversation stays within what it was about; when that is deleted, it stops rather than
   widening to the whole organisation.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED knowledge bases | `4909355` | 2 test modules fail to import; 3 eval and 2 web tests fail |
| GREEN | `2cf3c06` | 1,716 Python tests, 92%; web 65; e2e 9/9; answer eval 80 questions recorded, 0 outside the knowledge base; gate 47/47 |
| RED review findings | `e60e678` | 4 Python and 1 web test fail |
| GREEN | `21ad947` | 1,721 Python tests; web 66; e2e 9/9 |

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | Knowledge bases created, uniquely named per organisation (case and spaces aside), listed with their size, renamed, deleted with their documents kept | `tests/integration/test_collections.py`, `test_api_collections.py` | integration |
| 2 | Documents added all or nothing, at most 500 a call, and removed; another organisation's never | `tests/integration/test_collections.py` | integration |
| 3 | Another organisation cannot see, change or ask a knowledge base | `tests/integration/test_collections.py` | integration |
| 4 | Search and questions within a knowledge base read only its documents | `tests/integration/test_collections.py` | integration |
| 5 | A question has one scope, kept on its conversation; a follow-up whose knowledge base or document is gone is refused (409), never widened | `tests/integration/test_collections.py` | integration |
| 6 | Reviewers read and ask; only those who may add documents change knowledge bases | `tests/integration/test_api_collections.py` | integration |
| 7 | The stream gives each stage, then the checked answer or an error; a failing listener never fails a question; two questions in flight per person | `tests/integration/test_api_collections.py`, `test_collections.py` | integration |
| 8 | Answers within a knowledge base come only from it | `evals/gate.json` over `evals/baselines/answers.json` (`outside_knowledge_base == 0`) | eval |
| 9 | Scope of a question, server-sent events read across chunks and at the end | `web/tests/unit/collections.test.ts` | unit |
| 10 | A knowledge base is made, given the SOP, and asked within | `web/tests/e2e/review.spec.ts` | e2e |

## Coverage and known gaps

`make test` 1,721 tests, 92%. See Honest limits under C12 in `docs/progress.md`.
