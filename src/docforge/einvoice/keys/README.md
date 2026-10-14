# Trusted IRP signing keys

Public keys of the e-invoice registration portals (IRPs), used to verify the signed QR on an
invoice. `manifest.json` lists each: where it came from, when, its file's sha256 (checked on
every load), its kid when a certificate gives one, and whether a real invoice's QR has
verified with it (`confirmed_by_real_qr`).

- NIC1, NIC2 and the NIC sandbox keys are as published in the GSP Vayana's documentation
  (https://docs.gsp.vayana.com/E-Invoice/_index/): a third party, not NIC itself. Until a
  real QR verifies with one, DocForge says the key is not yet confirmed.
- IRIS (einvoice6) is IRIS's own certificate from its public-keys page. It expired on
  2026-06-20 and is kept: QRs it signed while valid still verify.
- Keys of the other private IRPs (Clear, Cygnet, EY) were not found published; their QRs read
  as signed with a key DocForge does not hold.

To add or replace a key: put the PEM here, add its entry with its sha256, and confirm it with
a real invoice's QR before setting `confirmed_by_real_qr`.
