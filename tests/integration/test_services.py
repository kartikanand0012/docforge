"""The Compose services are reachable with the configured settings."""

import urllib.error
import urllib.request

import pytest
from sqlalchemy import create_engine, text

from docforge.config import Settings

pytestmark = pytest.mark.integration


def http_status(url: str) -> int:
    request = urllib.request.Request(url, method="HEAD")  # noqa: S310 - local http endpoint
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as error:
        return error.code


def test_postgres_is_version_16(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        version = conn.execute(text("SHOW server_version_num")).scalar_one()
    engine.dispose()

    assert 160000 <= int(version) < 170000


def test_object_store_is_live(settings: Settings) -> None:
    assert http_status(f"{settings.s3_endpoint_url}/minio/health/live") == 200


def test_originals_bucket_exists(settings: Settings) -> None:
    # Anonymous HEAD: 403 means the bucket exists but is private, 404 means it is missing.
    assert http_status(f"{settings.s3_endpoint_url}/{settings.s3_bucket}") == 403


def test_unknown_bucket_is_reported_missing(settings: Settings) -> None:
    assert http_status(f"{settings.s3_endpoint_url}/no-such-bucket-docforge") == 404
