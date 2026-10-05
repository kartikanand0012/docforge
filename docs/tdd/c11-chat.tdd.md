# TDD evidence: C11 chat with documents

Source plan: C11 in `docs/research/06-knowledge-chat-connectors.md` (owner's decisions of
2026-10-03: Gemini Flash-Lite answers, judged by the answer eval); P0 items of
`docs/research/08-feature-roadmap.md`.

## Journeys

1. As a reviewer, I ask a question about the document in front of me and get an answer with
   the quote it rests on, outlined on the page.
2. As anyone in the organisation, I ask across all our documents, follow up, come back to
   the conversation later, and delete it when I want.
3. When the documents do not hold the answer, I am told so, not given a guess.
4. As the organisation, chat costs what we allow: a daily limit for us, and for each person.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED chat | `61685c1` | 5 test modules fail to import |
| GREEN chat, API, web, answer eval | `c2b650b` | 1,681 Python tests, 92%; web 59; e2e 8/8; answer eval recorded live: 31/32 correct, 0 wrong |
| RED review findings | `94d0a45` | 20 Python and 2 web tests fail |
| GREEN | `38ab902` | 1,699 Python tests, 92%; web 61; e2e 8/8; answer eval 39/40 correct, 0 wrong, 0 unanswerable answered; gate 46/46 |

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | A quote counts only if it is in the passage it names, on word and number boundaries, and means something | `tests/unit/test_chat_verify.py` | unit |
| 2 | The blocks a quote came from are found, not short labels or number cells | `tests/unit/test_chat_verify.py` | unit |
| 3 | Passages, names, question and history are fenced; nothing in them opens or closes a tag | `tests/unit/test_chat_prompt.py` | unit |
| 4 | Supported, partly supported, withheld and not-found answers; a citation to a passage not given is no citation | `tests/integration/test_chat.py` | integration |
| 5 | One document or the whole organisation; follow-ups with the conversation so far, non-answers left out; a follow-up cannot switch document | `tests/integration/test_chat.py`, `test_api_chat.py` | integration |
| 6 | Conversations are their owner's, per tenant; deleted by their owner | `tests/integration/test_chat.py`, `test_api_chat.py` | integration |
| 7 | The daily limits (organisation, person) hold under concurrent questions and count failures with their tokens | `tests/integration/test_chat.py` | integration |
| 8 | Chat recording refused in production | `tests/unit/test_config.py` | unit |
| 9 | The answer eval scores strictly: one expected figure, boundaries, unanswerables answered counted | `tests/unit/test_eval_answers.py` | unit |
| 10 | Answer quality held by the gate: correct, cited, abstaining, no wrong answers, no leaks, cost | `evals/gate.json` over `evals/baselines/answers.json` | eval |
| 11 | Status wording, citation outlines, Enter while composing | `web/tests/unit/chat.test.ts` | unit |
| 12 | A Word SOP is asked about, its source is outlined on the page, and an unanswerable question is declined | `web/tests/e2e/review.spec.ts` | e2e |

## Coverage and known gaps

`make test` 1,699 tests, 92%. See Honest limits under C11 in `docs/progress.md`.
