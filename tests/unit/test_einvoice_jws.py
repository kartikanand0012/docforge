"""V2 1.1: the signed QR on an Indian e-invoice, verified offline against the IRPs' keys."""

import base64
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from docforge.einvoice.jws import MAX_QR_CHARS, verify
from docforge.einvoice.keys import KeyRing, KeyRingError, TrustedKey, load_keyring

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "einvoice"
DATA = {
    "SellerGstin": "27AAKCS4821M1ZW", "BuyerGstin": "24AABCU9603R1ZM", "DocNo": "INV/26-27/0042",
    "DocTyp": "INV", "DocDt": "02/09/2026", "TotInvVal": 112345.5, "ItemCnt": 7,
    "MainHsnCode": "30049099", "Irn": "a" * 64,
}  # fmt: skip


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNER = private_key()
OTHER = private_key()


def certificate(key: rsa.RSAPrivateKey, expired: bool = False) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DS TEST IRP")])
    now = datetime.now(UTC)
    start, end = (
        (now - timedelta(days=800), now - timedelta(days=30))
        if expired
        else (now, now + timedelta(days=365))
    )
    return (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(start).not_valid_after(end).sign(key, hashes.SHA256())
    )  # fmt: skip


def thumbprint(cert: x509.Certificate) -> str:
    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha1(der).hexdigest().upper()  # noqa: S324 - the IRPs' kid is a SHA-1 thumbprint


def signed(
    data: dict[str, Any] | str = DATA, *, key: rsa.RSAPrivateKey = SIGNER, kid: str | None = None,
    header: dict[str, Any] | None = None, iss: str = "NIC",
) -> str:  # fmt: skip
    head = header or {"alg": "RS256", "kid": kid or "0" * 40, "typ": "JWT"}
    body = {"data": data if isinstance(data, str) else json.dumps(data), "iss": iss}
    signing_input = f"{b64(json.dumps(head).encode())}.{b64(json.dumps(body).encode())}"
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{b64(signature)}"


def trusted(
    key: rsa.RSAPrivateKey = SIGNER, *, kid: str | None = None, name: str = "TEST-1"
) -> TrustedKey:
    return TrustedKey(
        name=name, irp="TEST", environment="production", source_url="https://example.test/key",
        fetched="2026-10-14", sha256="0" * 64, kid=kid, confirmed_by_real_qr=False, not_after=None,
        public_key=key.public_key(),
    )  # fmt: skip


def test_a_genuine_qr_verifies_and_gives_its_typed_fields() -> None:
    result = verify(signed(), KeyRing((trusted(),)))

    assert result.status == "verified"
    assert result.key is not None and result.key.name == "TEST-1"
    assert result.issuer == "NIC"
    invoice = result.invoice
    assert invoice is not None
    assert (invoice.seller_gstin, invoice.buyer_gstin) == ("27AAKCS4821M1ZW", "24AABCU9603R1ZM")
    assert (invoice.doc_no, invoice.doc_type) == ("INV/26-27/0042", "INV")
    assert invoice.doc_date == date(2026, 9, 2)
    assert invoice.total == Decimal("112345.5") and invoice.item_count == 7
    assert (invoice.main_hsn, invoice.irn) == ("30049099", "a" * 64)


def test_the_kid_picks_the_key_and_a_key_without_one_is_tried() -> None:
    cert = certificate(SIGNER)
    kid = thumbprint(cert)
    ring = KeyRing((trusted(OTHER, kid="F" * 40, name="OTHER"), trusted(kid=kid, name="RIGHT")))
    bare = KeyRing((trusted(OTHER, name="BARE-A"), trusted(name="BARE-B")))

    picked = verify(signed(kid=kid), ring).key
    tried = verify(signed(kid="1" * 40), bare).key

    assert picked is not None and picked.name == "RIGHT"
    assert tried is not None and tried.name == "BARE-B"


def test_a_changed_payload_does_not_verify() -> None:
    head, body, signature = signed(kid="A" * 40).split(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "=="))
    forged["data"] = forged["data"].replace("112345.5", "1.5")
    tampered = f"{head}.{b64(json.dumps(forged).encode())}.{signature}"

    result = verify(tampered, KeyRing((trusted(kid="A" * 40),)))

    assert result.status == "bad_signature"
    assert result.invoice is None  # nothing from an unverified payload is trusted


def test_a_qr_signed_by_a_key_we_do_not_hold_is_unknown_not_bad() -> None:
    result = verify(signed(key=OTHER, kid="B" * 40), KeyRing((trusted(kid="C" * 40),)))

    assert result.status == "unknown_key"
    assert result.kid == "B" * 40


@pytest.mark.parametrize("alg", ["none", "HS256", "RS512", "PS256"])
def test_only_rs256_is_accepted(alg: str) -> None:
    text = signed(header={"alg": alg, "kid": "A" * 40})

    assert verify(text, KeyRing((trusted(kid="A" * 40),))).status == "malformed"


def test_an_expired_certificate_still_verifies_what_it_signed() -> None:
    cert = certificate(SIGNER, expired=True)
    key = trusted(kid=thumbprint(cert))

    assert verify(signed(kid=thumbprint(cert)), KeyRing((key,))).status == "verified"


@pytest.mark.parametrize(
    "text",
    [
        "upi://pay?pa=shop@okaxis&pn=Shop&am=100",
        "https://vendor.example/invoice/42",
        "IRN a1b2c3",
        "",
    ],
)
def test_a_qr_that_is_not_an_e_invoice_says_so(text: str) -> None:
    assert verify(text, KeyRing((trusted(),))).status == "not_einvoice"


@pytest.mark.parametrize(
    "text",
    [
        "abc.def.ghi",  # three parts, not base64url JSON
        signed("not json"),
        signed({**DATA, "DocDt": "2026-09-02"}),
        signed({**DATA, "Irn": "xyz"}),
        signed({**DATA, "DocTyp": "PROFORMA"}),
        signed({**DATA, "TotInvVal": "a lot"}),
        signed({key: value for key, value in DATA.items() if key != "SellerGstin"}),
        signed({**DATA, "Pad": "x" * 3000}),
    ],
)
def test_a_malformed_qr_is_refused_by_name(text: str) -> None:
    result = verify(text, KeyRing((trusted(),)))

    assert result.status == "malformed"
    assert result.invoice is None
    assert result.reason


def test_an_oversized_qr_is_refused_before_it_is_parsed() -> None:
    assert verify("a." * (MAX_QR_CHARS // 2) + "b", KeyRing((trusted(),))).status == "malformed"


def test_an_unregistered_buyer_is_read() -> None:
    result = verify(signed({**DATA, "BuyerGstin": "URP"}), KeyRing((trusted(),)))

    assert result.status == "verified"
    assert result.invoice is not None and result.invoice.buyer_gstin == "URP"


def test_the_real_nic_sandbox_qr_verifies_with_its_published_key() -> None:
    pem = (FIXTURES / "nic_sandbox_2020_public_key.pem").read_bytes()
    key = serialization.load_pem_public_key(pem)
    assert isinstance(key, rsa.RSAPublicKey)
    ring = KeyRing((
        TrustedKey(name="NIC-SANDBOX-2020", irp="NIC", environment="sandbox",
                   source_url="https://docs.cleartax.in", fetched="2026-10-14",
                   sha256=hashlib.sha256(pem).hexdigest(), kid=None, confirmed_by_real_qr=True,
                   not_after=None, public_key=key),
    ))  # fmt: skip

    result = verify((FIXTURES / "nic_sandbox_qr.txt").read_text().strip(), ring)

    assert result.status == "verified"
    assert result.invoice is not None
    assert (result.invoice.doc_no, result.invoice.doc_date) == ("DD-20200804-9", date(2020, 8, 4))
    assert result.invoice.total == Decimal("7776.3") and result.invoice.item_count == 3


# --- the bundled key set -------------------------------------------------------------------


def write_manifest(directory: Path, entries: list[dict[str, Any]]) -> None:
    (directory / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")


def pem_file(directory: Path, name: str, key: rsa.RSAPrivateKey = SIGNER) -> str:
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    (directory / name).write_bytes(pem)
    return hashlib.sha256(pem).hexdigest()


def manifest_entry(
    file: str, sha256: str, environment: str = "production", **overrides: Any
) -> dict[str, Any]:
    return {
        "name": file, "irp": "TEST", "environment": environment, "file": file,
        "source_url": "https://example.test", "fetched": "2026-10-14", "sha256": sha256,
        "kid": None, "confirmed_by_real_qr": False, "not_after": None, **overrides,
    }  # fmt: skip


def test_the_key_set_loads_only_its_environment(tmp_path: Path) -> None:
    write_manifest(tmp_path, [
        manifest_entry("prod.pem", pem_file(tmp_path, "prod.pem")),
        manifest_entry("sandbox.pem", pem_file(tmp_path, "sandbox.pem", OTHER), "sandbox"),
    ])  # fmt: skip

    production = load_keyring("production", tmp_path)

    assert [key.name for key in production.keys] == ["prod.pem"]
    assert [key.name for key in load_keyring("sandbox", tmp_path).keys] == ["sandbox.pem"]


def test_a_key_file_that_does_not_match_the_manifest_refuses_to_load(tmp_path: Path) -> None:
    pem_file(tmp_path, "prod.pem")
    write_manifest(tmp_path, [manifest_entry("prod.pem", "0" * 64)])

    with pytest.raises(KeyRingError, match=r"prod\.pem"):
        load_keyring("production", tmp_path)


def test_a_certificate_gives_its_kid_and_a_wrong_manifest_kid_is_refused(tmp_path: Path) -> None:
    cert = certificate(SIGNER)
    pem = cert.public_bytes(serialization.Encoding.PEM)
    (tmp_path / "irp.crt").write_bytes(pem)
    sha = hashlib.sha256(pem).hexdigest()

    write_manifest(tmp_path, [manifest_entry("irp.crt", sha, kid=thumbprint(cert))])
    (key,) = load_keyring("production", tmp_path).keys
    assert key.kid == thumbprint(cert)

    write_manifest(tmp_path, [manifest_entry("irp.crt", sha, kid="0" * 40)])
    with pytest.raises(KeyRingError, match="kid"):
        load_keyring("production", tmp_path)


@pytest.mark.parametrize("file", ["../escape.pem", "/etc/passwd", "sub/key.pem"])
def test_a_manifest_naming_a_file_outside_its_folder_is_refused(tmp_path: Path, file: str) -> None:
    write_manifest(tmp_path, [manifest_entry(file, "0" * 64)])

    with pytest.raises(KeyRingError, match="file"):
        load_keyring("production", tmp_path)


def test_the_bundled_production_keys_load_and_none_is_a_sandbox_or_test_key() -> None:
    ring = load_keyring("production")

    assert {key.irp for key in ring.keys} >= {"NIC1", "NIC2", "IRIS"}
    assert all(key.environment == "production" for key in ring.keys)
    assert all(key.source_url.startswith("https://") for key in ring.keys)
    assert all(key.public_key.key_size >= 2048 for key in ring.keys)


# --- security review -------------------------------------------------------------------------


@pytest.mark.parametrize("total", ["NaN", "Infinity", "-Infinity", "-1", "1E+308", "1e13"])
def test_a_signed_total_that_is_not_a_real_amount_is_malformed_not_a_crash(total: str) -> None:
    data = json.dumps(DATA).replace("112345.5", total)

    result = verify(signed(data), KeyRing((trusted(),)))

    assert result.status == "malformed"


def test_the_signed_total_is_read_exactly_as_printed() -> None:
    data = json.dumps(DATA).replace("112345.5", "112345.50")

    result = verify(signed(data), KeyRing((trusted(),)))

    assert result.invoice is not None and str(result.invoice.total) == "112345.50"


@pytest.mark.parametrize(
    "changed",
    [
        {"DocNo": "INV\u0007042"},  # a control character
        {"DocNo": "INV-2026-0000000042"},  # more than 16 characters
        {"DocNo": "INV 42"},
        {"DocDt": "2/9/2026"},  # not dd/mm/yyyy
    ],
)
def test_the_invoice_number_and_date_are_held_to_the_gst_format(changed: dict[str, str]) -> None:
    assert verify(signed({**DATA, **changed}), KeyRing((trusted(),))).status == "malformed"


def test_the_issuer_is_given_only_when_verified_and_kept_short() -> None:
    forged = verify(
        signed(key=OTHER, kid="A" * 40, iss="<script>"), KeyRing((trusted(kid="A" * 40),))
    )
    long = verify(signed(iss="N" * 500), KeyRing((trusted(),)))

    assert forged.status == "bad_signature" and forged.issuer is None
    assert long.issuer is not None and len(long.issuer) <= 32


def test_the_result_says_whether_the_key_was_confirmed_by_a_real_qr() -> None:
    unconfirmed = verify(signed(), KeyRing((trusted(),)))
    confirmed_key = TrustedKey(**{**trusted().__dict__, "confirmed_by_real_qr": True})
    confirmed = verify(signed(), KeyRing((confirmed_key,)))

    assert unconfirmed.status == confirmed.status == "verified"
    assert unconfirmed.key_confirmed is False and confirmed.key_confirmed is True
    assert verify("upi://pay", KeyRing((trusted(),))).key_confirmed is False
