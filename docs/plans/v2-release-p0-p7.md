# Release: V2 P0-P7 to production

Owner's go-ahead: 2026-11-05 ("take everything to production and make sure everything is
working as expected").

## What ships

| Phase | Backend | Web | Migration |
| --- | --- | --- | --- |
| QA fix + P0 visible flags | 2e18702, 3b78418 | 228451f..90ec61f | 0043 |
| P1 mapping master | 4c0111c | 786dfaa..60a577e | 0044 |
| P3 Marg import profiles (sample profile only, no Marg claim) | b223fd2 | 292c523..4ad42cc | 0045 |
| P4 purchase register + 2B check | 68810e8 | e4e9a6f..2c5bc8f | - |
| P5 invoices by email (off until its settings are set) | 43ac258 | 3e0756f..d41a272 | 0046 |
| P6 bulk upload + PDF split | c4f0143 | 5554952 | 0047 |
| P7 PO Variance Report | c6fc85d | 612f8b3 | 0048 |

**Not shipped:** P2 Tally XML (not built: waits on a real Tally purchase export). The 50-file
drop limit stays at 50.

## Steps

1. **Checks on the release commits.** Backend: full suite, lint, eval, gate, `alembic heads`
   single head, migrations 0042 -> 0048 -> 0042 -> 0048 (down and up). Web: `npm run check`,
   build.
2. **Rehearsal on production's data.** Back up the production database (pg_dump, custom
   format, kept off the repo). Restore the copy into a throwaway local Postgres 16 with
   pgvector, run `alembic upgrade head` on it, time it, and check the new tables, columns and
   row-level security. Nothing touches production in this step.
3. **Rehearsal on the local demo stack.** Rebuild its images from the release commits, run
   the admin role (migrations), then `make verify` end to end over HTTPS.
4. **Merge.** One release PR per repo (`v2-p7-variance-report` -> `main`, a merge commit so
   each phase's commit stays). CI green before merge.
5. **Deploy, in order.** admin (runs 0043..0048, roles, erasure; exits) -> api -> worker ->
   converter -> web. Each waits for the one before to succeed.
6. **Check production.** Health, the admin log ends "admin: done" at head 0048, then a live
   walk-through as a test account: upload a bill and its PO, review, the flags, the mappings,
   the purchase register and PO Variance files, a ZIP drop, a two-invoice PDF split, settings
   tolerances. Another workspace sees none of it. API and worker logs free of errors.

## Rollback

- Code: Railway redeploys the previous deployment of each service (web first, then api,
  worker, converter).
- Schema: every migration is reversible; `alembic downgrade 0042` from the admin image, or,
  if data is wrong, restore the step-2 backup.
- The new migrations only add tables and columns with defaults, so the previous code runs
  on the new schema; a code rollback does not need a schema rollback.

## After release (needs the owner)

- Email intake: set INTAKE_DOMAIN, INTAKE_WEBHOOK_PASSWORD, SMTP_URL, INTAKE_REPLY_FROM,
  WEB_URL and the MX record, then the Postmark inbound webhook.
- One real bill + PO through the variance report (P7's mind-change test).
- A real Marg import before any Marg claim; P2 Tally decision.

## Log

(filled in as the release runs)
