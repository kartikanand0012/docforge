"""Signing PINs, the hash a signature covers, and the draft an approval produces.

The PIN is a second factor at the moment of signing: the reviewer re-enters it for each
signature, so a signature shows the named person acted then. Designed to support, not
replace, an organisation's own electronic-signature controls.
"""

import hashlib
import hmac
import json
import secrets
from datetime import datetime
from typing import Any

from docforge.extraction.schema import InvoiceExtraction

_N, _R, _P = 2**14, 8, 1  # scrypt cost: about 16 MB and tens of milliseconds per check


def _scrypt(pin: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, n=n, r=r, p=p, dklen=32)


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(pin, salt, _N, _R, _P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        expected = bytes.fromhex(digest)
        actual = _scrypt(pin, bytes.fromhex(salt), int(n), int(r), int(p))
    except ValueError:
        return False
    return scheme == "scrypt" and hmac.compare_digest(actual, expected)


# What a signature means, per document type and outcome. Fixed here so a client cannot have
# someone sign a sentence of its own choosing.
MEANINGS: dict[str, dict[str, str]] = {
    "invoice": {
        "approved": "I approve this invoice for payment",
        "rejected": "I reject this invoice",
    },
    "purchase_order": {
        "approved": "I approve this purchase order record",
        "rejected": "I reject this purchase order record",
    },
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()


def record_digest(record: dict[str, Any]) -> str:
    """SHA-256 of a record alone: what the reviewer was shown, to compare before signing."""
    return hashlib.sha256(_canonical(record)).hexdigest()


def record_hash(record: dict[str, Any], **signed: Any) -> str:
    """SHA-256 of what is signed: the record and everything said about it (who, which
    document and version, the decision, its meaning, the reasons, and when)."""
    canonical = json.dumps(
        {"record": record, **signed},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def approval_draft(
    invoice: InvoiceExtraction, *, reviewer: str, signed_at: datetime, record_sha256: str
) -> dict[str, Any]:
    """What an approved invoice turns into: a payment approval for a person or a system to
    act on. Nothing is paid here."""
    total = invoice.totals.grand_total.value
    return {
        "type": "payment_approval_draft",
        "status": "draft",
        "payee": {"name": invoice.seller.name.value, "gstin": invoice.seller.gstin.value},
        "invoice_no": invoice.invoice_no.value,
        "invoice_date": invoice.invoice_date.value.isoformat()
        if invoice.invoice_date.value
        else None,
        "amount": str(total) if total is not None else None,
        "currency": "INR",
        "po_no": invoice.po_no.value,
        "approved_by": reviewer,
        "approved_at": signed_at.isoformat(),
        "record_sha256": record_sha256,
    }
