# Electronic records and signatures

How DocForge's controls are designed to support 21 CFR Part 11 and EU GMP Annex 11, and where
the organisation's own procedures must complete them. This is a mapping for the validating
organisation, not a claim of compliance.

| Expectation | Reference | Designed to support it with | The organisation provides |
| --- | --- | --- | --- |
| Validation | 11.10(a); Annex 11 §4 | This pack, the automated tests, the measured evals and the gate | Its own validation for its intended use |
| Accurate and complete copies | 11.10(b); Annex 11 §8 | The original kept as uploaded; signed records exported as CSV and JSON; page images of the original | — |
| Record protection and retrieval | 11.10(c); Annex 11 §7 | Append-only tables; content-addressed originals; versioned storage | Backups, retention periods, restore tests |
| Limited system access | 11.10(d); Annex 11 §12 | Accounts per person and per system; roles; row-level security per organisation | Granting and removing access; periodic access review |
| Audit trail | 11.10(e); Annex 11 §9 | Every action recorded with who, what and when, append-only, hash-chained and anchored outside the database; corrections keep old value, new value and reason | Review of the audit trail as part of its procedures |
| Sequencing of steps | 11.10(f) | A record is signed only after review; a stale record cannot be signed; a document's version history is kept | — |
| Authority checks | 11.10(g) | Roles decide who may upload, correct, sign or administer | Who is given which role |
| Training | 11.10(i); Annex 11 §2 | — | Training records for reviewers and administrators |
| Signature manifestation | 11.50; Annex 11 §14 | A signature shows the signer's name, the date and time, and its meaning (fixed per document type and outcome) | — |
| Signature linked to the record | 11.70 | A signature covers a hash of the exact record signed and cannot be moved to another | — |
| Unique signatures | 11.100(a) | Each reviewer has their own account, which belongs to one organisation | Not reusing or reassigning accounts |
| Identity verification | 11.100(b) | — | Verifying identity before an account is made |
| Certification to the FDA | 11.100(c) | — | The organisation's letter of certification |
| Two signature components | 11.200(a) | Signed-in session plus the PIN entered again at each signature | A PIN policy; deciding if this meets its needs |
| Controls on identification codes | 11.300 | PINs stored only as scrypt hashes; lockout after five wrong attempts; sessions expire | PIN changes and periodic review |

## Known gaps

- No PIN expiry or reuse history: PIN policy is the organisation's.
- No second factor beyond the PIN at sign-in; single sign-on is not built.
- Time comes from the server clock; the organisation keeps its hosting time-synchronised.
- The demo deployment has no backups; a production deployment must add them.
