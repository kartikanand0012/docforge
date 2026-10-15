"""V2 1.1 part 7: the IRPs' keys as NIC and IRIS publish them, confirmed against real QRs."""

import base64
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from docforge.einvoice.jws import verify
from docforge.einvoice.keys import load_keyring

OFFICIAL = ("https://einvoice1.gst.gov.in/", "https://einvoice2.gst.gov.in/",
            "https://einvoice6.gst.gov.in/")  # fmt: skip
# Kids with which real production invoices' QRs, published in public code, verified (2026-10-15).
CONFIRMED = {
    "115F4426617A7938BE1BA06DBEE91A427584EDAB",  # NIC 2018-2020: 3 QRs
    "B8D63ECB58EA5E6F4AAC3D524543B2246669B0F8",  # NIC IRP-1 2022-2024: 112 QRs
    "4DE1544AE695BDC84EC7BC12F2F57F813F44E301",  # NIC IRP-1 2024-2026: 212 QRs
}
NIC1_2024 = "4DE1544AE695BDC84EC7BC12F2F57F813F44E301"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_every_production_key_is_an_official_certificate_with_its_kid_and_expiry() -> None:
    ring = load_keyring("production")

    assert {key.irp for key in ring.keys} >= {"NIC1", "NIC2", "IRIS"}
    for key in ring.keys:
        assert key.source_url.startswith(OFFICIAL), key.name
        assert key.kid is not None and len(key.kid) == 40, key.name
        assert key.not_after is not None, key.name


def test_the_keys_real_qrs_verified_with_are_confirmed_and_no_others() -> None:
    ring = load_keyring("production")

    assert {key.kid for key in ring.keys if key.confirmed_by_real_qr} == CONFIRMED


def test_the_keys_nic_signs_with_now_and_next_are_held() -> None:
    kids = {key.kid for key in load_keyring("production").keys}

    assert NIC1_2024 in kids
    assert {
        "DEB2031A279F203BBDDF3538E77D4122ACB4D07D",  # NIC IRP-2 2024-2026
        "CD01AC2B6E720CF0B9E048F1CCBC7509C766AF78",  # NIC IRP-1 2025-2027
        "644956C46E4E947AAD72FD15DFDB01EB9A96A66B",  # NIC IRP-2 2025-2027
    } <= kids


def test_an_altered_nic_qr_is_a_bad_signature_now_that_its_kid_is_held() -> None:
    forger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    head = b64(json.dumps({"alg": "RS256", "kid": NIC1_2024, "typ": "JWT"}).encode())
    body = b64(json.dumps({"data": "{}", "iss": "NIC"}).encode())
    signature = forger.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())

    result = verify(f"{head}.{body}.{b64(signature)}", load_keyring("production"))

    assert result.status == "bad_signature"


def test_no_sandbox_key_is_also_a_production_key() -> None:
    production = {key.kid for key in load_keyring("production").keys}
    sandbox = load_keyring("sandbox").keys

    assert sandbox
    assert not production & {key.kid for key in sandbox}
