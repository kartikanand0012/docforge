"""The signed QR of an e-invoice, verified offline.

An IRP prints a compact JWS: `base64url(header).base64url(payload).base64url(signature)`, RS256
(RSA PKCS#1 v1.5 over SHA-256). The header's `kid` is the hex SHA-1 thumbprint of the signing
certificate; the payload is `{"data": "<JSON string>", "iss": "NIC"}`, the data holding the
seller's and buyer's GSTIN, the document's number, type and date, its total, how many items it
has, its main HSN code and its IRN.

Nothing in a QR is believed before its signature verifies with a key we trust; the text is
from an untrusted page, so every part has a size limit and only RS256 is accepted.
"""

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from pydantic import BaseModel, ConfigDict

from docforge.einvoice.keys import KeyRing, TrustedKey

MAX_QR_CHARS = 4096  # a signed QR is about 900
MAX_DATA_CHARS = 2048

Status = Literal["verified", "bad_signature", "unknown_key", "malformed", "not_einvoice"]
DocType = Literal["INV", "CRN", "DBN"]

_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_GSTIN = re.compile(r"[0-9A-Z]{15}")
_IRN = re.compile(r"[0-9a-fA-F]{64}")


class SignedInvoice(BaseModel):
    """What the IRP signed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seller_gstin: str
    buyer_gstin: str  # "URP" for an unregistered buyer
    doc_no: str
    doc_type: DocType
    doc_date: date
    total: Decimal
    item_count: int
    main_hsn: str | None
    irn: str
    irn_date: str | None


@dataclass(frozen=True)
class Verification:
    status: Status
    reason: str
    invoice: SignedInvoice | None = None  # only when verified
    key: TrustedKey | None = None  # the key that verified it
    kid: str | None = None  # as the QR names it
    issuer: str | None = None


class _Malformed(ValueError):
    pass


def _decode(segment: str) -> Any:
    try:
        return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (binascii.Error, ValueError) as error:
        raise _Malformed("a part of it is not base64url JSON") from error


def _text(data: dict[str, Any], field: str, pattern: re.Pattern[str] | None = None) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise _Malformed(f"{field} is missing or not text")
    if pattern is not None and not pattern.fullmatch(value.strip()):
        raise _Malformed(f"{field} is not in its format")
    return value.strip()


def _number(data: dict[str, Any], field: str) -> int | float:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _Malformed(f"{field} is not a number")
    return value


def _optional(value: Any, limit: int) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    return str(value)[:limit]


def _invoice(raw: Any) -> SignedInvoice:
    if not isinstance(raw, str) or len(raw) > MAX_DATA_CHARS:
        raise _Malformed("its data is missing or too long")
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise _Malformed("its data is not JSON") from error
    if not isinstance(data, dict):
        raise _Malformed("its data is not an object")
    buyer = _text(data, "BuyerGstin")
    if buyer != "URP" and not _GSTIN.fullmatch(buyer):
        raise _Malformed("BuyerGstin is not in its format")
    doc_type = _text(data, "DocTyp")
    if doc_type not in ("INV", "CRN", "DBN"):
        raise _Malformed("DocTyp is not INV, CRN or DBN")
    try:
        doc_date = datetime.strptime(_text(data, "DocDt"), "%d/%m/%Y").date()
    except ValueError as error:
        raise _Malformed("DocDt is not dd/mm/yyyy") from error
    item_count = _number(data, "ItemCnt")
    if not isinstance(item_count, int) or item_count < 0:
        raise _Malformed("ItemCnt is not a count")
    return SignedInvoice(
        seller_gstin=_text(data, "SellerGstin", _GSTIN),
        buyer_gstin=buyer,
        doc_no=_text(data, "DocNo"),
        doc_type=doc_type,
        doc_date=doc_date,
        total=Decimal(str(_number(data, "TotInvVal"))),
        item_count=item_count,
        main_hsn=_optional(data.get("MainHsnCode"), 16),
        irn=_text(data, "Irn", _IRN),
        irn_date=_optional(data.get("IrnDt"), 32),
    )


def verify(text: str, keyring: KeyRing) -> Verification:
    """Whether `text` (a QR's content) is an e-invoice signed by a key in `keyring`."""
    text = text.strip()
    if len(text) > MAX_QR_CHARS:
        return Verification("malformed", f"longer than {MAX_QR_CHARS} characters")
    parts = text.split(".")
    if len(parts) != 3 or not all(_SEGMENT.fullmatch(part) for part in parts):
        return Verification("not_einvoice", "not a signed e-invoice QR")
    kid: str | None = None
    try:
        header, payload = _decode(parts[0]), _decode(parts[1])
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise _Malformed("its header or payload is not an object")
        if header.get("alg") != "RS256":
            raise _Malformed("it is not signed with RS256")
        named = header.get("kid")
        kid = named.upper()[:64] if isinstance(named, str) else None
        signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    except (_Malformed, binascii.Error, ValueError) as error:
        return Verification("malformed", str(error), kid=kid)
    issuer = payload.get("iss") if isinstance(payload.get("iss"), str) else None
    candidates, held = keyring.candidates(kid)
    signed = f"{parts[0]}.{parts[1]}".encode()
    for key in candidates:
        try:
            key.public_key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            continue
        try:
            invoice = _invoice(payload.get("data"))
        except _Malformed as error:
            reason = f"signed, but {error}"
            return Verification("malformed", reason, key=key, kid=kid, issuer=issuer)
        return Verification("verified", f"signed by {key.irp}", invoice, key, kid, issuer)
    if held:
        reason = "the signature does not match: the QR was altered or not issued by this IRP"
        return Verification("bad_signature", reason, kid=kid, issuer=issuer)
    reason = "signed with a key DocForge does not hold"
    return Verification("unknown_key", reason, kid=kid, issuer=issuer)
