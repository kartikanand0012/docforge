"""Conversion as a service of its own: LibreOffice runs in a container with no route to the
internet, the cloud's metadata service or DocForge's database and storage. The worker sends
it a file over an internal network and gets a PDF back."""

import httpx
import pytest
from fastapi.testclient import TestClient

from docforge.conversion import ConversionError, ConverterUnavailable
from docforge.conversion_service import RemoteConverter, create_converter_app
from docforge.formats import Format

TOKEN = "t" * 32


class Stub:
    version = "stub-1"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def to_pdf(self, data: bytes, fmt: Format) -> bytes:
        self.calls.append(fmt)
        if data == b"bad":
            raise ConversionError("LibreOffice could not read the file.")
        if data == b"down":
            raise ConverterUnavailable("LibreOffice is not installed on this worker.")
        return b"%PDF-converted"


def service(stub: Stub | None = None, max_bytes: int = 1024) -> TestClient:
    return TestClient(create_converter_app(stub or Stub(), TOKEN, max_bytes=max_bytes))


def convert(
    client: httpx.Client, data: bytes, fmt: str = "docx", token: str = TOKEN
) -> httpx.Response:
    return client.post(
        f"/v1/convert?format={fmt}", content=data, headers={"Authorization": f"Bearer {token}"}
    )


def test_a_file_is_converted_for_a_caller_with_the_token() -> None:
    response = convert(service(), b"docx bytes")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == b"%PDF-converted"


@pytest.mark.parametrize("token", ["", "wrong", "t" * 31])
def test_without_the_token_nothing_is_converted(token: str) -> None:
    stub = Stub()
    assert convert(service(stub), b"docx bytes", token=token).status_code == 401
    assert stub.calls == []


def test_a_file_that_cannot_be_converted_and_an_unavailable_converter_are_told_apart() -> None:
    client = service()
    refused = convert(client, b"bad")
    assert (
        refused.status_code == 422
        and refused.json()["detail"] == "LibreOffice could not read the file."
    )
    assert convert(client, b"down").status_code == 503


def test_formats_it_does_not_convert_and_files_over_the_limit_are_refused() -> None:
    client = service(max_bytes=16)
    assert convert(client, b"x", fmt="exe").status_code == 422
    assert convert(client, b"x", fmt="pdf").status_code == 422  # a PDF is not converted
    assert convert(client, b"x" * 17).status_code == 413


def test_it_says_its_version_and_that_it_is_up() -> None:
    client = service()
    assert client.get("/healthz").status_code == 200
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/v1/version", headers=headers).json() == {"version": "stub-1"}


# --- the worker's side --------------------------------------------------------------------


def remote(stub: Stub | None = None) -> RemoteConverter:
    return RemoteConverter("http://converter:8090", TOKEN, client=service(stub))


def test_the_worker_gets_the_pdf_and_the_service_version() -> None:
    converter = remote()
    assert converter.to_pdf(b"docx bytes", "docx") == b"%PDF-converted"
    assert converter.version == "stub-1"


def test_a_pdf_is_never_sent() -> None:
    stub = Stub()
    pdf = b"%PDF-1.7 as it is"
    assert remote(stub).to_pdf(pdf, "pdf") is pdf
    assert stub.calls == []


def test_the_services_answers_become_the_same_errors_as_local_conversion() -> None:
    converter = remote()
    with pytest.raises(ConversionError, match="could not read"):
        converter.to_pdf(b"bad", "docx")
    with pytest.raises(ConverterUnavailable):
        converter.to_pdf(b"down", "docx")


def test_a_service_that_cannot_be_reached_is_unavailable_not_the_files_fault() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    converter = RemoteConverter(
        "http://converter:8090", TOKEN, client=httpx.Client(transport=httpx.MockTransport(refuse))
    )
    with pytest.raises(ConverterUnavailable):
        converter.to_pdf(b"docx bytes", "docx")


def test_a_malformed_version_answer_is_unavailable_not_an_internal_error() -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy</html>")

    client = httpx.Client(base_url="http://converter:8090", transport=httpx.MockTransport(html))
    converter = RemoteConverter("http://converter:8090", TOKEN, client=client)
    with pytest.raises(ConverterUnavailable):
        _ = converter.version
