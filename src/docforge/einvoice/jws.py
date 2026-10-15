"""The signed QR of an e-invoice, verified offline.

An IRP prints a compact JWS: `base64url(header).base64url(payload).base64url(signature)`, RS256
(RSA PKCS#1 v1.5 over SHA-256). The header's `kid` is the hex SHA-1 thumbprint of the signing
certificate; the payload is `{"data": "<JSON string>", "iss": "NIC"}`, the data holding the
seller's and buyer's GSTIN, the document's number, type and date, its total, how many items it
has, its main HSN code and its IRN.

Nothing in a QR is believed before its signature verifies with a key we trust; the text is
from an untrusted page, so every part has a size limit and only RS256 is accepted. A verified
QR proves what the IRP registered, not that it belongs to the page it is printed on: a genuine
QR copied onto a forged invoice verifies, so its fields must be compared with the page's.
A key not yet confirmed by a real invoice's QR (`key_confirmed`) is weaker evidence.
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
from pydantic import BaseModel, ConfigDict, ValidationError

from docforge.einvoice.keys import KeyRing, TrustedKey

MAX_QR_CHARS = 4096  # a signed QR is about 900
MAX_DATA_CHARS = 2048
MAX_TOTAL = Decimal("1e12")  # INR; far above any invoice
MAX_ISSUER_CHARS = 32

Status = Literal["verified", "bad_signature", "unknown_key", "malformed", "not_einvoice"]
DocType = Literal["INV", "CRN", "DBN"]

_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_GSTIN = re.compile(r"[0-9A-Z]{15}")
_IRN = re.compile(r"[0-9a-fA-F]{64}")
_DOC_NO = re.compile(r"[A-Za-z0-9/-]{1,16}")  # CGST Rule 46: at most 16, letters, digits, / and -
_DOC_DATE = re.compile(r"\d{2}/\d{2}/\d{4}")


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
    issuer: str | None = None  # only when verified

    @property
    def key_confirmed(self) -> bool:
        """Verified with a key that a real invoice's QR has been seen to verify with."""
        return self.key is not None and self.key.confirmed_by_real_qr


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


def _number(data: dict[str, Any], field: str) -> int | Decimal:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise _Malformed(f"{field} is not a number")
    return value


def _refuse_constant(name: str) -> None:
    raise _Malformed(f"its data holds {name}, not a number")


def _optional(value: Any, limit: int) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int) or "\x00" in str(value):
        return None  # a NUL cannot be stored (Postgres), and no real field holds one
    return str(value)[:limit]


def _invoice(raw: Any) -> SignedInvoice:
    if not isinstance(raw, str) or len(raw) > MAX_DATA_CHARS:
        raise _Malformed("its data is missing or too long")
    try:
        # Amounts as printed (Decimal, never float); NaN and Infinity refused.
        data = json.loads(raw, parse_float=Decimal, parse_constant=_refuse_constant)
    except _Malformed:
        raise
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
        doc_date = datetime.strptime(_text(data, "DocDt", _DOC_DATE), "%d/%m/%Y").date()
    except ValueError as error:
        raise _Malformed("DocDt is not dd/mm/yyyy") from error
    total = Decimal(_number(data, "TotInvVal"))
    if not total.is_finite() or not Decimal(0) <= total < MAX_TOTAL:
        raise _Malformed("TotInvVal is not an amount")
    item_count = _number(data, "ItemCnt")
    if not isinstance(item_count, int) or item_count < 0:
        raise _Malformed("ItemCnt is not a count")
    return SignedInvoice(
        seller_gstin=_text(data, "SellerGstin", _GSTIN),
        buyer_gstin=buyer,
        doc_no=_text(data, "DocNo", _DOC_NO),
        doc_type=doc_type,
        doc_date=doc_date,
        total=total,
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
    claimed = payload.get("iss")
    issuer = claimed[:MAX_ISSUER_CHARS] if isinstance(claimed, str) else None
    candidates, held = keyring.candidates(kid)
    signed = f"{parts[0]}.{parts[1]}".encode()
    for key in candidates:
        try:
            key.public_key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            continue
        try:
            invoice = _invoice(payload.get("data"))
        except (_Malformed, ValidationError) as error:
            reason = f"signed, but {error}"
            return Verification("malformed", reason, key=key, kid=kid)
        return Verification("verified", f"signed by {key.irp}", invoice, key, kid, issuer)
    if held:
        reason = "the signature does not match: the QR was altered or not issued by this IRP"
        return Verification("bad_signature", reason, kid=kid)
    # Bare keys were tried and failed too: altered, or signed by an IRP whose key we lack.
    reason = "signed with a key DocForge does not hold, or altered"
    if issuer is not None and issuer.upper().startswith("NIC"):
        # Unverified, so it only explains: DocForge holds every key NIC has published since
        # September 2022, so a recent QR naming NIC with another key deserves a closer look.
        reason += "; it names NIC as its issuer, but NIC has not published that key"
    return Verification("unknown_key", reason, kid=kid)
