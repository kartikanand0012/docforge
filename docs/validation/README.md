# Validation-support pack

DocForge is designed to support a regulated organisation's own computerised-system validation.
It is not validated, certified or compliant by itself. Validation is done by the organisation
using it, for its intended use, under its own quality system (for example following GAMP 5).
These documents give that organisation what a supplier normally provides.

| Document | What it gives the validating organisation |
| --- | --- |
| [Intended use](intended-use.md) | What the system is for, and what it is not for |
| [Requirements and traceability](traceability.md) | Each requirement, where it is built, and the automated test that shows it |
| [Risk assessment](risk-assessment.md) | What can go wrong, the controls in the system, and what is left to the organisation |
| [Electronic records and signatures](records-and-signatures.md) | How the system's controls map to 21 CFR Part 11 and EU GMP Annex 11, and the gaps |

Evidence the organisation can re-run:

- `make test`: the automated test suite (1,500+ tests) on every change, in CI.
- `make eval` and `make gate`: measured accuracy against labelled documents, with floors that
  block a worse change (`evals/gate.json`).
- `docs/progress.md`: what each checkpoint measured and its known limits.
- The audit trail's own check: `GET /v1/audit/verification`.

Version: this pack describes the code at the commit it is shipped with. A change to the system
is a change to these documents; the traceability table names the tests to re-run.
