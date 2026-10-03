"""Where webhooks may point, and how a delivery is signed."""

import hashlib
import hmac
import socket
import uuid

import pytest

from docforge.webhooks import UnsafeDestination, check_destination, derive_secret, sign


def resolving_to(*addresses: str):  # type: ignore[no-untyped-def]
    def resolve(host: str, port: int, *args: object, **kwargs: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in addresses
        ]

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "ftp://hooks.example.com/x",
        "https:///x",
        "not a url",
        "https://user:pw@hooks.example.com/",
    ],
)
def test_only_plain_https_urls_are_accepted(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", resolving_to("93.184.216.34"))

    with pytest.raises(UnsafeDestination):
        check_destination(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.0.10",
        "169.254.169.254",
        "172.16.0.1",
        "::1",
        "fc00::1",
        "0.0.0.0",  # noqa: S104 - an address to refuse, not to bind
    ],
)
def test_a_host_that_resolves_to_an_internal_address_is_refused(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cloud metadata address and the private ranges are not for webhooks."""
    monkeypatch.setattr(socket, "getaddrinfo", resolving_to(address))

    with pytest.raises(UnsafeDestination, match="not a public address"):
        check_destination("https://hooks.example.com/in")


def test_one_internal_address_among_public_ones_is_enough_to_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", resolving_to("93.184.216.34", "10.0.0.7"))

    with pytest.raises(UnsafeDestination):
        check_destination("https://hooks.example.com/in")


def test_a_public_https_destination_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", resolving_to("93.184.216.34"))

    check_destination("https://hooks.example.com/in?x=1")


def test_local_http_is_allowed_only_when_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", resolving_to("127.0.0.1"))

    check_destination("http://127.0.0.1:9999/in", allow_http=True, allow_private=True)
    with pytest.raises(UnsafeDestination):
        check_destination("http://127.0.0.1:9999/in", allow_private=True)


def test_each_webhook_has_its_own_secret_derived_from_the_server_key() -> None:
    key, first, second = b"k" * 32, uuid.uuid4(), uuid.uuid4()

    assert derive_secret(key, first, 1) == derive_secret(key, first, 1)
    assert derive_secret(key, first, 1) != derive_secret(key, second, 1)
    assert derive_secret(key, first, 1) != derive_secret(key, first, 2)  # rotation
    assert derive_secret(key, first, 1).startswith("whsec_")


def test_the_signature_covers_the_timestamp_and_the_exact_body() -> None:
    secret, body = "whsec_" + "a" * 64, b'{"id":"e1"}'

    header = sign(secret, body, timestamp=1_700_000_000)

    expected = hmac.new(secret.encode(), b"1700000000." + body, hashlib.sha256).hexdigest()
    assert header == f"t=1700000000,v1={expected}"


@pytest.mark.parametrize("address", ["64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "64:ff9b:1::1"])
def test_nat64_addresses_that_reach_internal_hosts_are_refused(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", resolving_to(address))

    with pytest.raises(UnsafeDestination):
        check_destination("https://hooks.example.com/in")


def test_the_checked_address_is_the_one_returned_for_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolving again when connecting would let a name change between check and use."""
    monkeypatch.setattr(socket, "getaddrinfo", resolving_to("93.184.216.34"))

    assert check_destination("https://hooks.example.com/in") == "93.184.216.34"
