"""Conversion as a service of its own, so LibreOffice never runs where DocForge's network,
credentials and data are.

In the deployed stack the converter sits on an internal network that only the worker shares:
no route to the internet, the cloud's metadata service, the database or storage. It reads
only its token, and converts with the same limits as ever (`IsolatedConverter`).

    python -m docforge.conversion_service    # listens on 0.0.0.0:8090

The worker uses `RemoteConverter`, which turns the service's answers into the errors local
conversion raises, so the document service cannot tell them apart.
"""

import hmac
import os
from functools import partial
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response

from docforge.conversion import (
    ConversionError,
    Converter,
    ConverterUnavailable,
    FileConverter,
    IsolatedConverter,
)
from docforge.formats import FORMATS, Format

DEFAULT_MAX_BYTES = 12 * 1024 * 1024  # a little above the largest upload
_CONVERTED: frozenset[str] = frozenset(FORMATS) - {"pdf"}


def converter_version(converter: object) -> str:
    return str(getattr(converter, "version", "unknown"))


def create_converter_app(
    converter: Converter, token: str, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> FastAPI:
    app = FastAPI(title="DocForge converter", docs_url=None, redoc_url=None, openapi_url=None)
    expected = f"Bearer {token}".encode()

    def authorised(authorization: str | None) -> None:
        if not hmac.compare_digest((authorization or "").encode(), expected):
            raise HTTPException(401, "Not authorised.")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/version")
    def version(authorization: Annotated[str | None, Header()] = None) -> dict[str, str]:
        authorised(authorization)
        return {"version": converter_version(converter)}

    @app.post("/v1/convert")
    async def convert(
        request: Request,
        fmt: Annotated[str, Query(alias="format")],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        authorised(authorization)
        if fmt not in _CONVERTED:
            raise HTTPException(422, "This format is not converted.")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > max_bytes:
                raise HTTPException(413, "The file is too large.")
        try:
            pdf = converter.to_pdf(bytes(body), fmt)  # type: ignore[arg-type]
        except ConversionError as error:
            raise HTTPException(422, str(error)) from error
        except ConverterUnavailable as error:
            raise HTTPException(503, str(error)) from error
        return Response(pdf, media_type="application/pdf")

    return app


class RemoteConverter:
    """A `Converter` that asks the converter service."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_seconds: float = 210.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._client = client or httpx.Client(base_url=url, timeout=timeout_seconds)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._version: str | None = None

    @property
    def version(self) -> str:
        """The service's converter, asked once: a converted PDF is kept under it, so a new
        LibreOffice converts again."""
        if self._version is None:
            response = self._send("GET", "/v1/version")
            self._version = str(response.json()["version"])
        return self._version

    def to_pdf(self, data: bytes, fmt: Format) -> bytes:
        if fmt == "pdf":
            return data
        response = self._send("POST", "/v1/convert", params={"format": fmt}, content=data)
        return response.content

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, headers=self._headers, **kwargs)
        except httpx.HTTPError as error:
            raise ConverterUnavailable("The converter could not be reached.") from error
        if response.status_code == 422:
            raise ConversionError(_detail(response, "The file could not be converted."))
        if response.status_code == 413:
            raise ConversionError("The file is too large to convert.")
        if response.status_code != 200:
            raise ConverterUnavailable(f"The converter answered {response.status_code}.")
        return response


def _detail(response: httpx.Response, default: str) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        return default
    return detail if isinstance(detail, str) else default


def main() -> None:  # pragma: no cover - the container's entry point
    import uvicorn

    token = os.environ.get("CONVERTER_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("CONVERTER_TOKEN must be set (32 characters or more)")
    timeout = float(os.environ.get("CONVERSION_TIMEOUT_SECONDS", "120"))
    converter = IsolatedConverter(
        partial(FileConverter, timeout_seconds=timeout),
        timeout_seconds=timeout + 30,
        max_rss_bytes=int(os.environ.get("CONVERSION_MAX_RSS_MB", "2048")) * 1024 * 1024,
    )
    uvicorn.run(create_converter_app(converter, token), host="0.0.0.0", port=8090)  # noqa: S104


if __name__ == "__main__":  # pragma: no cover
    main()
