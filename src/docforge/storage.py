"""Object storage for original files. S3 in production, MinIO locally."""

import uuid
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from docforge.config import Settings

_MISSING = {"404", "NoSuchKey", "NotFound"}


class ObjectNotFound(Exception):
    """No object is stored under the key."""


class StorageUnavailable(Exception):
    """The store could not be reached or refused the request. Trying again later may work."""


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes:
        """Raises `ObjectNotFound` if nothing is stored under `key`."""
        ...

    def exists(self, key: str) -> bool: ...


def original_key(tenant_id: uuid.UUID, sha256: str) -> str:
    """Where an uploaded file lives. Content-addressed, so the same bytes map to one object."""
    return f"originals/{tenant_id}/{sha256}.pdf"


class S3ObjectStore:
    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3ObjectStore":
        config = Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3})
        if settings.s3_endpoint_url is None:
            # AWS: credentials from the default chain (the instance's role), never a key here.
            client = boto3.client("s3", region_name=settings.s3_region, config=config)
        else:
            client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
                region_name=settings.s3_region,
                config=config,
            )
        return cls(client, settings.s3_bucket)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageUnavailable("could not store the object") from error

    def get(self, key: str) -> bytes:
        try:
            body: bytes = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as error:
            if error.response["Error"]["Code"] in _MISSING:
                raise ObjectNotFound(key) from error
            raise StorageUnavailable("could not read the object") from error
        except BotoCoreError as error:  # connection refused, timeouts
            raise StorageUnavailable("could not read the object") from error
        return body

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] in _MISSING:
                return False
            raise StorageUnavailable("could not check for the object") from error
        except BotoCoreError as error:
            raise StorageUnavailable("could not check for the object") from error
        return True


class MemoryObjectStore:
    """For tests."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._objects[key] = data

    def get(self, key: str) -> bytes:
        try:
            return self._objects[key]
        except KeyError:
            raise ObjectNotFound(key) from None

    def exists(self, key: str) -> bool:
        return key in self._objects

    def keys(self) -> list[str]:
        return sorted(self._objects)

    def delete(self, key: str) -> None:
        del self._objects[key]
