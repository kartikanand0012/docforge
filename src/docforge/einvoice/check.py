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
        try:
            scan = self.scanner.parse(pdf)
        except Exception as error:
            # Untrusted pixels into native code: any failure is a result, never the reading's.
            logger.warning("e-invoice QR scan failed: %s", type(error).__name__)
            return QrCheck("unreadable", None, None, None, 0, None)
        evidence = evidence_from((found.text for found in scan.found), self.keyring)
        if evidence is None:
            return QrCheck("none", None, None, None, scan.pages_scanned, scan.decoder)
        chosen = scan.found[evidence.qr_index]
        return QrCheck(
            evidence.status, evidence, chosen.text, chosen.page, scan.pages_scanned, scan.decoder
        )
