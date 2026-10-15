"""Signed e-invoice QRs for an invoice of a test world, signed with a test-only key."""

import base64
import json
from datetime import date
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from docforge.einvoice.check import EinvoiceChecker
from docforge.einvoice.keys import KeyRing, TrustedKey
from docforge.einvoice.scan import FoundQr, QrScan
from worlds import World

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "A" * 40
RING = KeyRing(
    (
        TrustedKey(
            name="TEST-1", irp="TEST", environment="production", source_url="https://x.test",
            fetched="2026-10-15", sha256="0" * 64, kid=KID, confirmed_by_real_qr=False,
            not_after=None, public_key=KEY.public_key(),
        ),
    )
)  # fmt: skip


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def qr_for(world: World, **changes: Any) -> str:
    """A QR signed for the world's invoice, with `changes` to its signed fields."""
    invoice = json.loads(world.label_text)["invoice"]
    fields = {
        "SellerGstin": invoice["seller"]["gstin"], "BuyerGstin": invoice["buyer"]["gstin"],
        "DocNo": invoice["invoice_no"], "DocTyp": "INV",
        "DocDt": date.fromisoformat(invoice["invoice_date"]).strftime("%d/%m/%Y"),
        "TotInvVal": float(invoice["totals"]["grand_total"]), "ItemCnt": len(invoice["lines"]),
        "MainHsnCode": None, "Irn": "b" * 64,
    } | changes  # fmt: skip
    head = b64(json.dumps({"alg": "RS256", "kid": KID}).encode())
    body = b64(json.dumps({"data": json.dumps(fields), "iss": "NIC"}).encode())
    signature = KEY.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(signature)}"


def altered(qr: str) -> str:
    """`qr` with its signed total changed after signing."""
    head, body, signature = qr.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["data"] = json.dumps(json.loads(payload["data"]) | {"TotInvVal": 1.0})
    return f"{head}.{b64(json.dumps(payload).encode())}.{signature}"


class Scanner:
    """Finds `texts` on page 1, or raises `error`."""

    def __init__(self, *texts: str, error: Exception | None = None) -> None:
        self.texts, self.error, self.calls = texts, error, 0

    def parse(self, pdf: bytes) -> QrScan:
        self.calls += 1
        if self.error is not None:
            raise self.error
        found = tuple(FoundQr(page=1, text=text) for text in self.texts)
        return QrScan(found=found, pages_scanned=1, decoder="zxing-cpp test")


def scanned(world: World, scanner: Scanner) -> World:
    world.einvoice = EinvoiceChecker(scanner, RING)
    return world
