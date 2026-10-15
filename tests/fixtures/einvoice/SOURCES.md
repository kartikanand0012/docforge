# e-invoice QR test fixtures

- `nic_sandbox_qr.txt`: a signed QR from NIC's e-invoice API sandbox documentation (an
  invoice of 04/08/2020 between sandbox GSTINs), as reproduced at
  https://github.com/Pranav-error/evidence-verifier (`eval/fixtures/nic_sandbox_qr.txt`).
- `nic_sandbox_2020_public_key.pem`: the NIC sandbox signing key that verifies it, as published
  by ClearTax at https://docs.cleartax.in/cleartax-docs/e-invoicing-api/learn-e-invoicing-api-basics.
  NIC replaced its sandbox key on 23/02/2023; this one is for tests only and is never bundled.

Fetched 2026-10-14. Sandbox data only: no real taxpayer is named.
