"""An invoice's QR codes scanned and checked, as one step that never fails a reading.

Whatever goes wrong - the decoder crashes, runs out of time or memory, or the file cannot be
rendered - the check is `unreadable`: the invoice is still read, only without e-invoice rules.
"""

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from docforge.einvoice.evidence import QrEvidence, evidence_from
from docforge.einvoice.keys import KeyRing
from docforge.einvoice.scan import QrScan

logger = logging.getLogger(__name__)

CheckStatus = Literal["verified", "bad_signature", "malformed", "unknown_key", "none", "unreadable"]


class Scanner(Protocol):
    def parse(self, pdf: bytes) -> QrScan: ...


@dataclass(frozen=True)
class QrCheck:
    status: CheckStatus  # none: no signed-looking QR; unreadable: the scan itself failed
    evidence: QrEvidence | None
    qr_text: str | None  # the QR the evidence came from, kept to be checked again later
    page: int | None
    pages_scanned: int
    decoder: str | None


class EinvoiceChecker:
    def __init__(self, scanner: Scanner, keyring: KeyRing) -> None:
        self.scanner = scanner
        self.keyring = keyring

    def check(self, pdf: bytes) -> QrCheck:
        """The invoice's signed QR, if any; never raises."""
        try:
            return self._check(pdf)
        except Exception as error:
            # Untrusted pixels into native code, then untrusted text into verification: any
            # failure is a result, never the reading's.
            logger.warning("e-invoice QR check failed: %s", type(error).__name__)
            return QrCheck(
                status="unreadable", evidence=None, qr_text=None, page=None, pages_scanned=0,
                decoder=None,
            )  # fmt: skip

    def _check(self, pdf: bytes) -> QrCheck:
        scan = self.scanner.parse(pdf)
        # Postgres stores no NUL in text: such a QR is no e-invoice's, and kept it would fail
        # the reading's transaction.
        found = [qr for qr in scan.found if "\x00" not in qr.text]
        evidence = evidence_from((qr.text for qr in found), self.keyring)
        if evidence is None:
            return QrCheck(
                status="none", evidence=None, qr_text=None, page=None,
                pages_scanned=scan.pages_scanned, decoder=scan.decoder,
            )  # fmt: skip
        chosen = found[evidence.qr_index]
        return QrCheck(
            status=evidence.status, evidence=evidence, qr_text=chosen.text, page=chosen.page,
            pages_scanned=scan.pages_scanned, decoder=scan.decoder,
        )  # fmt: skip
