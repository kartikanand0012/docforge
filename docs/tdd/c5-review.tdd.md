# TDD evidence: C5 review

Source plan: checkpoint C5 in `docs/build-plan.md`; journeys derived during this run.

## Journeys

1. As a reviewer, I see which documents need me and why.
2. As a reviewer, I see each value next to where it came from on the page, and correct or confirm
   it with a reason.
3. As a reviewer, I approve or reject under my signature, and an approved invoice becomes a
   payment approval draft.
4. As an auditor, every correction and signature is attributed, explained, bound to what was
   signed, and cannot be changed afterwards.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| RED review logic | `ff33c9e` | collection fails: `docforge.review` missing |
| GREEN | `f84932b` | unit 1010 passed |
| RED review service | `2690fd1` | collection fails |
| GREEN | `07d4545` | `test_review.py` 19 passed |
| RED review API | `760e506` | `create_app` takes no review |
| GREEN | `c7ed311` | 1191 passed |
| Review screen and browser test | `d0753a7` | web checks; `scripts/e2e.sh` 5 passed (web helpers' unit tests were written after the code) |
| RED review findings | `f5b3b2a` | collection fails: `RecordChanged` missing |
| GREEN | `7b577d2` | review tests 31 passed; e2e 5 passed |

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | A correction replaces printed text, keeps the citation, is read again and re-checked; an unreadable one is reported | `test_review_logic.py` | unit |
| 2 | Re-entering a value confirms it and settles a citation doubt | `test_review_logic.py` | unit |
| 3 | PINs are salted scrypt hashes; the signature hash covers record, outcome, meaning and more | `test_review_logic.py`, `test_review.py` | unit, integration |
| 4 | Queue holds only documents needing a person, unsigned, not crowded out by others | `test_review.py` | integration |
| 5 | Wrong PIN changes nothing but the audit trail; five lock the reviewer; unknown, wrong and locked answer alike | `test_review.py`, `test_api_review.py` | integration |
| 6 | Approval over open checks needs an override reason, in code and in the database | `test_review.py` | integration |
| 7 | A signature binds to the record shown; a changed or superseded record is refused | `test_review.py`, `test_api_review.py` | integration |
| 8 | Signed versions cannot be corrected or signed again, in code and in the database | `test_review.py` | integration |
| 9 | Altering a stored review is detected | `test_review.py` | integration |
| 10 | Order corrections count when the invoice is checked against it | `test_review.py` | integration |
| 11 | Another tenant cannot see or review a document | `test_review.py` | integration |
| 12 | Page images are served; missing pages are 404; internal errors are 500, not 404 | `test_api_review.py` | integration |
| 13 | The eval summary reflects the committed reports and invents no price | `test_api_review.py` | integration |
| 14 | Boxes are placed on the page image correctly and clipped to it; field labels read well | `web/tests/unit` | unit (Vitest) |
| 15 | The whole review in a browser, with the audit trail and a reopened signed document | `web/tests/e2e/review.spec.ts` | end to end |

## Coverage and known gaps

`make test` 1,247 passed, 94%. Not covered: concurrent reviewers in real threads; multi-page
review in the browser; an automated accessibility audit.
