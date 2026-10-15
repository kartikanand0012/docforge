"""What an invoice's QR codes say about it, in a form that can be stored and read back.

Of the QR codes found on an invoice, only those shaped like a signed e-invoice count; a UPI
QR or a link says nothing about the invoice. Of those, the strongest is kept: a verified one,
else one whose signature fails with a key we hold (altered), else one we could not read, else
one signed by a key we do not hold; and whether an altered one sat beside a verified one,
which is tamper evidence although the verified one's fields are still compared. The raw QR
text is not kept here: it can be stored with the check, but the rules need only its outcome
and, when verified, its signed fields.
"""

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from docforge.einvoice.jws import SignedInvoice, verify
from docforge.einvoice.keys import KeyRing
from docforge.einvoice.scan import looks_signed

EvidenceStatus = Literal["verified", "bad_signature", "malformed", "unknown_key"]
_STRENGTH: tuple[EvidenceStatus, ...] = ("verified", "bad_signature", "malformed", "unknown_key")


class QrEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EvidenceStatus
    reason: str = Field(max_length=200)
    invoice: SignedInvoice | None  # only when verified
    irp: str | None  # the IRP whose key verified it
    key_name: str | None
    key_confirmed: bool  # that key has verified a real invoice's QR
    qr_index: int = Field(ge=0)  # which of the QR codes found it was
    altered_beside: bool = False  # another QR on the document was altered


def evidence_from(qrs: Iterable[str], keyring: KeyRing) -> QrEvidence | None:
    """The strongest signed-looking QR among `qrs`; None when there is none."""
    found: list[QrEvidence] = []
    for index, text in enumerate(qrs):
        if not looks_signed(text.strip()):
            continue
        result = verify(text, keyring)
        if result.status == "not_einvoice":
            continue
        key = result.key if result.status == "verified" else None
        found.append(
            QrEvidence(
                status=result.status,
                reason=result.reason[:200],
                invoice=result.invoice if key else None,
                irp=key.irp if key else None,
                key_name=key.name if key else None,
                key_confirmed=key is not None and key.confirmed_by_real_qr,
                qr_index=index,
            )
        )
    if not found:
        return None
    strongest = min(found, key=lambda evidence: _STRENGTH.index(evidence.status))
    altered = any(evidence.status == "bad_signature" for evidence in found)
    if strongest.status == "verified" and altered:
        return strongest.model_copy(update={"altered_beside": True})
    return strongest
