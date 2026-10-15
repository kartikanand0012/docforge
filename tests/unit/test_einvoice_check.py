"""V2 1.1 part 3: an invoice's QR codes scanned and checked; a failure is a result, not an error."""

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from docforge.einvoice.check import EinvoiceChecker
from docforge.einvoice.keys import KeyRing, TrustedKey
from docforge.einvoice.scan import FoundQr, IsolatedScanner, QrScan
from docforge.parsing.base import ParserLimitExceeded

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
DATA = {
    "SellerGstin": "24AABFN7754K1Z0", "BuyerGstin": "24AAHFJ1196Q1ZU", "DocNo": "NVM/26-27/1",
    "DocTyp": "INV", "DocDt": "02/09/2026", "TotInvVal": 100.0, "ItemCnt": 1,
    "MainHsnCode": "3004", "Irn": "a" * 64,
}  # fmt: skip
UPI = "upi://pay?pa=shop@okaxis&pn=Shop&am=100.00&cu=INR"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def signed() -> str:
    head = b64(json.dumps({"alg": "RS256", "kid": KID}).encode())
    body = b64(json.dumps({"data": json.dumps(DATA), "iss": "NIC"}).encode())
    signature = KEY.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(signature)}"


class Scanner:
    def __init__(self, *found: FoundQr, error: Exception | None = None) -> None:
        self.found, self.error = found, error

    def parse(self, pdf: bytes) -> QrScan:
        if self.error is not None:
            raise self.error
        return QrScan(found=self.found, pages_scanned=2, decoder="zxing-cpp 3")


def test_a_verified_qr_gives_its_evidence_text_and_page() -> None:
    qr = signed()
    check = EinvoiceChecker(Scanner(FoundQr(page=1, text=UPI), FoundQr(page=2, text=qr)), RING)

    result = check.check(b"%PDF")

    assert result.status == "verified"
    assert result.evidence is not None and result.evidence.irp == "TEST"
    assert (result.qr_text, result.page) == (qr, 2)
    assert (result.pages_scanned, result.decoder) == (2, "zxing-cpp 3")


@pytest.mark.parametrize("found", [(), (FoundQr(page=1, text=UPI),)])
def test_no_signed_qr_is_none_with_no_evidence(found: tuple[FoundQr, ...]) -> None:
    result = EinvoiceChecker(Scanner(*found), RING).check(b"%PDF")

    assert (result.status, result.evidence, result.qr_text, result.page) == (
        "none", None, None, None
    )  # fmt: skip


@pytest.mark.parametrize(
    "error", [ParserLimitExceeded("too slow"), ValueError("bad"), RuntimeError("crash")]
)
def test_a_scan_that_fails_is_unreadable_never_an_error(error: Exception) -> None:
    result = EinvoiceChecker(Scanner(error=error), RING).check(b"%PDF")

    assert (result.status, result.evidence, result.qr_text) == ("unreadable", None, None)
    assert result.pages_scanned == 0


def test_an_altered_qr_is_kept_with_its_text_for_a_later_look() -> None:
    head, _, signature = signed().split(".")
    altered = f"{head}.{b64(json.dumps({'data': '{}', 'iss': 'NIC'}).encode())}.{signature}"

    result = EinvoiceChecker(Scanner(FoundQr(page=1, text=altered)), RING).check(b"%PDF")

    assert result.status == "bad_signature"
    assert result.qr_text == altered and result.page == 1


# --- wiring ---------------------------------------------------------------------------------


def test_the_check_is_on_by_default_with_the_production_keys() -> None:
    from docforge.config import Settings
    from docforge.wiring import build_einvoice

    checker = build_einvoice(Settings(_env_file=None))

    assert isinstance(checker, EinvoiceChecker)
    assert {key.environment for key in checker.keyring.keys} == {"production"}
    assert isinstance(checker.scanner, IsolatedScanner)
    assert checker.scanner.timeout_seconds == 60


def test_the_kill_switch_turns_the_check_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from docforge.config import Settings
    from docforge.wiring import build_einvoice

    monkeypatch.setenv("EINVOICE_ENABLED", "false")

    assert build_einvoice(Settings(_env_file=None)) is None


def test_sandbox_keys_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    from docforge.config import Settings
    from docforge.wiring import build_einvoice

    monkeypatch.setenv("EINVOICE_KEYS", "sandbox")
    checker = build_einvoice(Settings(_env_file=None))

    assert checker is not None
    assert {key.environment for key in checker.keyring.keys} == {"sandbox"}
