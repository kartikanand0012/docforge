"""The audit hash must be reproducible from an entry's stored fields alone."""

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from docforge.audit import canonical_json, entry_hash

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
WHEN = datetime(2026, 10, 2, 9, 30, 15, 123456, tzinfo=UTC)
FIELDS: dict[str, Any] = {
    "prev_hash": "a" * 64,
    "tenant_id": TENANT,
    "occurred_at": WHEN,
    "actor": "api:upload",
    "action": "document.received",
    "target_type": "document",
    "target_id": "doc-1",
    "details": {"sha256": "b" * 64, "size_bytes": 10},
}


def test_same_fields_give_the_same_hash() -> None:
    assert entry_hash(**FIELDS) == entry_hash(**FIELDS)
    assert len(entry_hash(**FIELDS)) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prev_hash", "c" * 64),
        ("prev_hash", None),
        ("tenant_id", uuid.UUID("00000000-0000-0000-0000-000000000002")),
        ("occurred_at", WHEN + timedelta(microseconds=1)),
        ("actor", "api:other"),
        ("action", "document.deleted"),
        ("target_type", "extraction"),
        ("target_id", "doc-2"),
        ("details", {"sha256": "b" * 64, "size_bytes": 11}),
    ],
)
def test_changing_any_field_changes_the_hash(field: str, value: object) -> None:
    assert entry_hash(**{**FIELDS, field: value}) != entry_hash(**FIELDS)


def test_the_hash_does_not_depend_on_time_zone_or_key_order() -> None:
    same_instant = WHEN.astimezone(timezone(timedelta(hours=5, minutes=30)))
    reordered = {"size_bytes": 10, "sha256": "b" * 64}

    assert entry_hash(**{**FIELDS, "occurred_at": same_instant, "details": reordered}) == (
        entry_hash(**FIELDS)
    )


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="time zone"):
        entry_hash(**{**FIELDS, "occurred_at": WHEN.replace(tzinfo=None)})


def test_canonical_json_is_stable_and_compact() -> None:
    assert (
        canonical_json({"b": 1, "a": {"d": [1, 2], "c": "é"}}) == '{"a":{"c":"é","d":[1,2]},"b":1}'
    )
