"""Signing PINs, the hash a signature covers, and the draft an approval produces.

The PIN is a second factor at the moment of signing: the reviewer re-enters it for each
signature, so a signature shows the named person acted then. Designed to support, not
replace, an organisation's own electronic-signature controls.
"""

import hashlib
import hmac
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docforge.extraction.schema import InvoiceExtraction

# argon2id at OWASP's minimum: 19 MiB, two passes, one lane. New PINs and passwords are
# hashed so; older scrypt hashes are still checked and replaced on the next right entry.
_ARGON2 = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1, hash_len=32,
                         salt_len=16, type=Type.ID)  # fmt: skip
ARGON2_PREFIX = "$argon2id$"


def _scrypt(pin: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, n=n, r=r, p=p, dklen=32)


# Each hash takes 19 MiB: at most this many are worked out at once in a process, so many
# sign-ins together cannot take memory without bound. The rest wait their turn, briefly.
_HASHING = threading.BoundedSemaphore(4)
HASHING_WAIT_SECONDS = 5.0


class HashingBusy(Exception):
    """Every place for working out a hash stayed taken for too long: try again shortly."""


@contextmanager
def _hashing_place() -> Iterator[None]:
    places = _HASHING
    if not places.acquire(timeout=HASHING_WAIT_SECONDS):
        raise HashingBusy
    try:
        yield
    finally:
        places.release()


def hash_pin(pin: str) -> str:
    """A PIN's or a password's hash, as argon2id. Raises `HashingBusy`."""
    with _hashing_place():
        return _ARGON2.hash(pin)


def verify_pin(pin: str, stored: str) -> bool:
    """Whether `pin` is the secret `stored` is the hash of: argon2id, or scrypt (older).
    Raises `HashingBusy`."""
    with _hashing_place():
        if stored.startswith(ARGON2_PREFIX):
            try:
                return _ARGON2.verify(stored, pin)
            except (VerificationError, InvalidHashError):
                return False
        try:
            scheme, n, r, p, salt, digest = stored.split("$")
            expected = bytes.fromhex(digest)
            actual = _scrypt(pin, bytes.fromhex(salt), int(n), int(r), int(p))
        except ValueError:
            return False
        return scheme == "scrypt" and hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    """Whether a hash is older or weaker than what `hash_pin` makes now."""
    if not stored.startswith(ARGON2_PREFIX):
        return True
    try:
        return _ARGON2.check_needs_rehash(stored)
    except InvalidHashError:
        return True


def upgrade_hash(session: Session, reviewer_id: uuid.UUID, stored: str, secret: str) -> None:
    """After `secret` was found right for `stored`: replace an older hash with argon2id, in
    the caller's transaction. Through the owner's function: the application's role cannot
    write a reviewer's hash itself, and the function changes only this one, only from the
    hash it had, in the workspace in scope."""
    if needs_rehash(stored):
        session.execute(select(func.docforge_upgrade_secret(reviewer_id, stored, hash_pin(secret))))


# What a signature means, per document type and outcome. Fixed here so a client cannot have
# someone sign a sentence of its own choosing.
MEANINGS: dict[str, dict[str, str]] = {
    "invoice": {
        "approved": "I approve this invoice for payment",
        "rejected": "I reject this invoice",
    },
    "coa": {
        "approved": "I accept this certificate of analysis",
        "rejected": "I reject this certificate of analysis",
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
    act on. Nothing is paid here.

    The draft is not part of what a signature covers (the record and what is said about
    it are) and is made after the signature's hash, so its fields can grow without voiding
    any signature."""
    total = invoice.totals.grand_total.value
    return {
        "type": "payment_approval_draft",
        # The draft's own lifecycle: DocForge never pays; the ERP acts on the draft.
        "status": "draft",
        "approval_status": "approved",  # a draft is made only for an approval
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
