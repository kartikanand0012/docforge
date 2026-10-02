"""The S3 object store against the Compose MinIO, and the in-memory stand-in."""

import uuid

import pytest

from docforge.config import Settings
from docforge.db import DEFAULT_TENANT_ID
from docforge.storage import (
    MemoryObjectStore,
    ObjectNotFound,
    ObjectStore,
    S3ObjectStore,
    StorageUnavailable,
    original_key,
)

pytestmark = pytest.mark.integration


@pytest.fixture(params=["s3", "memory"])
def store(request: pytest.FixtureRequest, settings: Settings) -> ObjectStore:
    if request.param == "memory":
        return MemoryObjectStore()
    return S3ObjectStore.from_settings(settings)


def unique_key() -> str:
    return f"tests/{uuid.uuid4()}.bin"


def test_stores_and_returns_bytes_unchanged(store: ObjectStore) -> None:
    key, data = unique_key(), bytes(range(256)) * 4

    store.put(key, data, "application/octet-stream")

    assert store.get(key) == data
    assert store.exists(key)


def test_a_missing_object_is_reported(store: ObjectStore) -> None:
    key = unique_key()

    assert not store.exists(key)
    with pytest.raises(ObjectNotFound):
        store.get(key)


def test_putting_the_same_key_again_keeps_one_object(store: ObjectStore) -> None:
    key = unique_key()

    store.put(key, b"first", "application/pdf")
    store.put(key, b"first", "application/pdf")

    assert store.get(key) == b"first"


def test_original_key_is_built_from_tenant_and_content_hash() -> None:
    assert original_key(DEFAULT_TENANT_ID, "ab" * 32) == (
        f"originals/{DEFAULT_TENANT_ID}/{'ab' * 32}.pdf"
    )


def test_an_unreachable_store_is_reported_as_unavailable(settings: Settings) -> None:
    unreachable = settings.model_copy(update={"s3_endpoint_url": "http://127.0.0.1:1"})
    store = S3ObjectStore.from_settings(unreachable)

    with pytest.raises(StorageUnavailable):
        store.exists("anything")
    with pytest.raises(StorageUnavailable):
        store.put("anything", b"x", "application/pdf")
    with pytest.raises(StorageUnavailable):
        store.get("anything")
