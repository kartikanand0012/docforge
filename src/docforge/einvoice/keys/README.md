# Trusted IRP signing keys

Public keys of the e-invoice registration portals (IRPs), used to verify the signed QR on an
invoice. `manifest.json` lists each: where it came from, when, its file's sha256 (checked on
every load), its kid (the SHA-1 thumbprint of its certificate, which a QR's header names) and
whether a real invoice's QR has verified with it (`confirmed_by_real_qr`).

- **NIC IRP-1 and IRP-2**: NIC's own signing certificates, from the downloads of
  einvoice1.gst.gov.in (`/Documents/*.zip`), fetched 2026-10-15. Each portal lists the period
  each key signed in; every key the portals list from September 2022 on is here, including
  the 2026-2027 ones. The periods in the keys' names are those listings (when NIC switched
  keys), not the certificates' validity. A key that has expired is kept: QRs it signed while
  valid still verify, and expiry is not checked when verifying.
- **Confirmed** (2026-10-15) against real production invoices' QRs published in public code:
  NIC IRP-1 2022-2024 (112 QRs) and 2024-2026 (212 QRs). The others are NIC's own files but
  no real QR has verified with them yet.
- **Not bundled**: NIC's 2020-2022 key (`ProdPubKey3`, a bare key that verified none of the
  2020-2021 QRs found, which name kid `444405C7...`), and NIC's August-September 2020 keys,
  from before e-invoicing went live on 1 October 2020. A QR from 2020-2022 reads as signed with
  a key DocForge does not hold: a warning, not a failure.
- **IRIS** (einvoice6): IRIS's own certificate from its public-keys page. It expired on
  2026-06-20 and is kept: QRs it signed while valid still verify.
- Keys of the other private IRPs (Clear, Cygnet, EY) were not found published; their QRs read
  as signed with a key DocForge does not hold.
- **Sandbox** keys load only in the sandbox environment and can never vouch for a real invoice.
- A production key must be a certificate: its kid is then checked against it, and there is no
  bare production key to try for a kid DocForge does not hold.

To add or replace a key: put the PEM certificate here (from the IRP's own site), add its entry
with its sha256 and kid, and set `confirmed_by_real_qr` only once a real invoice's QR verifies
with it.
