"""The one form a received mail takes inside DocForge, whatever brought it (V2 P5).

A provider's adapter (`intake.postmark`) or the provider-agnostic route turns its payload into
an `InboundEmail`; nothing past that point knows which provider it was.
"""

import base64
import binascii
from dataclasses import dataclass, field

MAX_ATTACHMENTS = 20  # a mail with more is read for its first 20


@dataclass(frozen=True)
class InboundAttachment:
    filename: str
    content_type: str | None
    data: bytes
    inline: bool = False  # shown in the body (a logo, a signature) rather than attached


@dataclass(frozen=True)
class InboundEmail:
    sender: str
    recipients: tuple[str, ...]
    subject: str
    message_id: str | None = None
    attachments: tuple[InboundAttachment, ...] = field(default=())


class PayloadInvalid(ValueError):
    """A provider's payload that is not a mail we can read, said in words."""


def decode(content: str | None) -> bytes:
    """An attachment's base64 content. Raises `PayloadInvalid`."""
    try:
        return base64.b64decode(content or "", validate=False)
    except (binascii.Error, ValueError) as error:
        raise PayloadInvalid("An attachment's content is not base64.") from error


def address_of(value: str | None) -> str:
    """`Asha <asha@x.in>` or `asha@x.in` as `asha@x.in`, lower case."""
    text = (value or "").strip()
    if "<" in text and text.endswith(">"):
        text = text[text.rindex("<") + 1 : -1]
    return text.strip().lower()
