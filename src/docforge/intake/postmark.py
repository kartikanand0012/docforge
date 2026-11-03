"""Postmark's inbound webhook, as an `InboundEmail` (V2 P5): the only Postmark code there is.

Fields read (Postmark's inbound JSON): `FromFull.Email` (or `From`), `ToFull[].Email`,
`CcFull[].Email`, `BccFull[].Email` and `OriginalRecipient` (the address the mail was
delivered to, whatever the headers say), `Subject`, `MessageID`, and `Attachments[]` of
`Name`, `Content` (base64), `ContentType` and `ContentID` (set when shown inline).
"""

from typing import Any

from docforge.intake.model import (
    MAX_ATTACHMENTS,
    InboundAttachment,
    InboundEmail,
    PayloadInvalid,
    address_of,
    decode,
)


def _emails(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return []
    return [address_of(e.get("Email")) for e in entries if isinstance(e, dict) and e.get("Email")]


def from_postmark(payload: Any) -> InboundEmail:
    """Raises `PayloadInvalid`."""
    if not isinstance(payload, dict):
        raise PayloadInvalid("The payload is not a Postmark inbound message.")
    full = payload.get("FromFull")
    sender = address_of(full.get("Email") if isinstance(full, dict) else payload.get("From"))
    if not sender:
        raise PayloadInvalid("The message has no sender.")
    recipients = [address_of(payload.get("OriginalRecipient"))]
    for key in ("ToFull", "CcFull", "BccFull"):
        recipients += _emails(payload.get(key))
    attachments = []
    for entry in (payload.get("Attachments") or [])[:MAX_ATTACHMENTS]:
        if not isinstance(entry, dict):
            continue
        attachments.append(
            InboundAttachment(
                filename=str(entry.get("Name") or "attachment"),
                content_type=entry.get("ContentType"),
                data=decode(entry.get("Content")),
                inline=bool(entry.get("ContentID")),
            )
        )
    return InboundEmail(
        sender=sender,
        recipients=tuple(dict.fromkeys(r for r in recipients if r)),
        subject=str(payload.get("Subject") or ""),
        message_id=payload.get("MessageID"),
        attachments=tuple(attachments),
    )
