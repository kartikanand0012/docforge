"""On AWS the object store uses the default credential chain (the instance's role)."""

from docforge.config import Settings
from docforge.storage import S3ObjectStore


def test_without_an_endpoint_the_store_talks_to_aws_in_its_region() -> None:
    settings = Settings(s3_endpoint_url=None, s3_region="ap-south-1", s3_bucket="b")

    store = S3ObjectStore.from_settings(settings)

    client = store._client
    assert client.meta.region_name == "ap-south-1"
    assert "amazonaws.com" in client.meta.endpoint_url


def test_with_an_endpoint_the_store_uses_its_keys() -> None:
    store = S3ObjectStore.from_settings(Settings())

    assert store._client.meta.endpoint_url == "http://127.0.0.1:9000"
