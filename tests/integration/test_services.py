"""The Compose services are reachable with the configured settings."""

import urllib.error
import urllib.request
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
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


def s3_client(settings: Settings) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        region_name="us-east-1",
    )


def test_originals_bucket_exists(settings: Settings) -> None:
    response = s3_client(settings).head_bucket(Bucket=settings.s3_bucket)

    assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


def test_unknown_bucket_is_reported_missing(settings: Settings) -> None:
    with pytest.raises(ClientError) as error:
        s3_client(settings).head_bucket(Bucket="no-such-bucket-docforge")

    assert error.value.response["Error"]["Code"] == "404"
