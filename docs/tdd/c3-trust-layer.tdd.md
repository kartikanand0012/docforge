# TDD evidence: C3 trust layer

Source plan: checkpoint C3 in `docs/build-plan.md`; journeys derived during this run.

## Journeys

1. As a reviewer, I can see where on the page each extracted value came from, and I am told when
   a value is not in the text it cites.
2. As a buyer's accounts clerk, I am told when an invoice's arithmetic, dates, tax or identifiers
   are wrong, with the rule and the field named.
3. As a buyer, I am told when an invoice differs from the purchase order it quotes.
4. As an integrator, I get one decision per document, accept or review, with reasons, by API.
5. As the owner of the product, I can show that known defects are caught, and how often correct
   documents are sent to review.

## Checkpoint commits

| Stage | Commit | Evidence |
| --- | --- | --- |
| Verification and rules (tests written first, one commit) | `52a0c64` | unit tests for `trust/verify.py`, `trust/rules.py`, `trust/invoice_rules.py` pass |
| Document specs, purchase order, matching | `80317ce` | `test_purchase_order.py`, `test_trust_match.py` pass |
| Assessment | `b6842a9` | `test_trust_assess.py` passes |
| RED stored assessments, matching, API | `fe97f8f` | integration tests fail: tables and endpoint missing |
| GREEN stored assessments, matching, API | `8487cc0` | integration tests pass on Postgres 16 |
| Seeded-defect set (tests first) | `5d0708c` | `test_synth_seeded.py` passes; fixtures regenerate byte-identically |
| Live trust eval | `dc2970a` | first live run caught 8 of 9; after the product-name fix, 9 of 9 |
| RED first review round | `1b5dde2` | 9 failed, 77 passed in the three trust unit files |
| GREEN first review round | `eed612b` | 1,071 passed; eval replay unchanged |
| RED second review round | `50bba6e` | 5 failed, 73 passed in the four affected files |
| GREEN second review round | `f4dca5e` | 1,050 unit and integration tests passed; eval replay unchanged |
| RED database review | `155f09b` | 6 failed, 1 passed in `test_schema_c3.py` |
| GREEN database review | `1bd907f` | 149 integration tests passed; 1,084 in all |

The first three slices did not get separate RED commits: tests and code were committed together
after the tests had been run and seen to fail.

## Test specification

| # | What is guaranteed | Test | Type |
| --- | --- | --- | --- |
| 1 | A value found in its cited block is verified and carries that block's page box | `test_trust_verify.py` | unit |
| 2 | A value not in its cited block is flagged, with the blocks where it does appear | `test_trust_verify.py` | unit |
| 3 | A number is not found inside a longer number, a date, its negative or its bracketed form, nor assembled from two blocks | `test_trust_verify.py` | unit |
| 4 | A short number must be the whole cited block or one of the numbers in an all-numeric block | `test_trust_verify.py` | unit |
| 5 | No rule fails on any of the 20 correct invoices | `test_trust_rules.py` (parametrised) | unit |
| 6 | Each of 10 defect kinds fails exactly the rule that names it | `test_trust_rules.py` | unit |
| 7 | A rule with a missing input is "not evaluated", and that sends the document to review | `test_trust_rules.py`, `test_trust_assess.py` | unit |
| 8 | No discrepancy on any of the 20 correct pairs; lines pair by product, not position, case or spacing | `test_trust_match.py` | unit |
| 9 | Quantity, rate, free quantity, parties, order number and date differences are each reported with both values | `test_trust_match.py` | unit |
| 10 | Two lines of one product pair one to one, with the order line each agrees with | `test_trust_match.py` | unit |
| 11 | The assessment is stored once per version, is immutable, and is written to the audit log | `test_assessment.py` | integration |
| 12 | An invoice and its order are matched whichever arrives first; another supplier's order with the same number is not a counterpart; a replaced counterpart no longer counts | `test_assessment.py` | integration |
| 13 | An invoice with no order on file is `review` with `match_status` `no_counterpart` | `test_assessment.py` | integration |
| 14 | The API serves the assessment with page boxes and the match | `test_assessment.py` | integration |
| 15 | The committed trust report is reproduced offline, every seeded defect is caught, and the acceptance of correct pairs does not fall below 12 of 20 | `test_eval_trust.py` | unit, recorded model replies |
| 16 | Text on the page that looks like a block id cannot pass for one in the prompt | `test_prompt.py` | unit |
| 17 | A record cannot carry one tenant and point at another tenant's version; a match cannot span tenants and must pair an invoice with an order | `test_schema_c3.py` | integration |
| 18 | Migration 0005 applies to a database with existing records, downgrades and re-applies; the order-number lookup can use its index | `test_schema_c3.py` | integration |

## Coverage and known gaps

`make test`: 1,084 passed, coverage 95%. Not covered by tests: many documents sharing one order
number; scanned input; optimal (rather than greedy) pairing of duplicate lines.
