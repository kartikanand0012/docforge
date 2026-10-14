"""The IRPs' public signing keys DocForge trusts.

Each invoice registration portal (NIC's two, and the private IRPs since 2023) signs its QRs
with its own key, and replaces it every year or two. The keys are bundled under `keys/` with a
manifest saying where each came from, when, its file's sha256 (checked on every load, so an
altered file refuses to load) and whether a real QR has been seen to verify with it. A key that
expired still verifies what it signed while valid, so old keys are kept.

Production loads only production keys: a sandbox key can never vouch for a real invoice.
Keys made for tests are never bundled; tests build a `KeyRing` themselves.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

Environment = Literal["production", "sandbox"]
KEYS_DIR = Path(__file__).with_name("keys")
_MIN_BITS = 2048


class KeyRingError(ValueError):
    """The bundled key set is not as its manifest says."""


@dataclass(frozen=True)
class TrustedKey:
    name: str
    irp: str  # NIC1, NIC2, IRIS, ...
    environment: Environment
    source_url: str
    fetched: str  # YYYY-MM-DD
    sha256: str  # of the key file
    kid: str | None  # hex SHA-1 of the signing certificate; None for a bare key
    confirmed_by_real_qr: bool  # a QR from a real invoice has verified with it
    not_after: str | None  # YYYY-MM-DD, when the certificate is known
    public_key: rsa.RSAPublicKey


@dataclass(frozen=True)
class KeyRing:
    keys: tuple[TrustedKey, ...]

    def candidates(self, kid: str | None) -> tuple[tuple[TrustedKey, ...], bool]:
        """The keys to try for a QR naming `kid`, and whether the kid named a key we hold.

        A kid only chooses: it is never trusted on its own. A kid we do not hold leaves the
        keys known without one (published as bare keys) to try.
        """
        if kid:
            named = tuple(key for key in self.keys if key.kid and key.kid == kid.upper())
            if named:
                return named, True
        return tuple(key for key in self.keys if key.kid is None), False


def _plain_file(name: object) -> str:
    if not isinstance(name, str) or not name or Path(name).name != name or name.startswith("."):
        raise KeyRingError(f"the manifest names a file outside its folder: {name!r}")
    return name


def _read(raw: bytes, name: str) -> tuple[rsa.RSAPublicKey, str | None]:
    """The key in a PEM file, and its certificate's thumbprint when the file is one."""
    thumbprint: str | None = None
    try:
        if b"BEGIN CERTIFICATE" in raw:
            certificate = x509.load_pem_x509_certificate(raw)
            der = certificate.public_bytes(serialization.Encoding.DER)
            thumbprint = hashlib.sha1(der).hexdigest().upper()  # noqa: S324 - the IRPs' kid
            key: object = certificate.public_key()
        else:
            key = serialization.load_pem_public_key(raw)
    except ValueError as error:
        raise KeyRingError(f"{name} is not a PEM key or certificate") from error
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < _MIN_BITS:
        raise KeyRingError(f"{name} is not an RSA key of at least {_MIN_BITS} bits")
    return key, thumbprint


def load_keyring(environment: Environment, directory: Path = KEYS_DIR) -> KeyRing:
    """The keys of one environment, each checked against the manifest."""
    try:
        entries = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise KeyRingError(f"no readable key manifest in {directory}") from error
    keys = []
    for entry in entries:
        name = _plain_file(entry.get("file"))
        if entry.get("environment") != environment:
            continue
        raw = (directory / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            raise KeyRingError(f"{name} does not match the sha256 in the manifest")
        key, thumbprint = _read(raw, name)
        listed = entry.get("kid")
        if thumbprint is not None and listed is not None and listed.upper() != thumbprint:
            raise KeyRingError(f"{name}: the manifest's kid is not its certificate's thumbprint")
        kid = listed or thumbprint
        keys.append(
            TrustedKey(
                name=entry["name"],
                irp=entry["irp"],
                environment=environment,
                source_url=entry["source_url"],
                fetched=entry["fetched"],
                sha256=entry["sha256"],
                kid=kid.upper() if kid else None,
                confirmed_by_real_qr=bool(entry.get("confirmed_by_real_qr")),
                not_after=entry.get("not_after"),
                public_key=key,
            )
        )
    return KeyRing(tuple(keys))
