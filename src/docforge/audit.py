"""The audit log: append-only, and hash-chained so that changes to it are evident.

Each entry stores the hash of the entry before it for the same tenant, and its own hash
over that and its contents. The database refuses UPDATE, DELETE and TRUNCATE on the table
and allows each entry only one successor.

What the chain shows: an entry that was edited, or removed from the middle, by someone who
did not also recompute every later hash. `verify_chain` reports where the chain breaks.

What it does not show, so do not claim it:
- A rewrite by someone who can write to the database and recomputes the hashes. The hash is
  not keyed and nothing outside the database holds a copy of it.
- Entries removed from the end: a shorter chain still verifies.
Closing those needs the latest hash to be recorded somewhere the database owner cannot
change, which is not built yet.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from docforge.db.models import AuditEntry


def canonical_json(value: Any) -> str:
    """One fixed text form for a JSON value: sorted keys, no spaces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(
    *,
    prev_hash: str | None,
    tenant_id: uuid.UUID,
    occurred_at: datetime,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any],
) -> str:
    """SHA-256 over the previous hash and every stored field of the entry."""
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at must carry a time zone")
    material = [
        prev_hash,
        str(tenant_id),
        occurred_at.astimezone(UTC).isoformat(timespec="microseconds"),
        actor,
        action,
        target_type,
        target_id,
        details,
    ]
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def append(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any],
) -> AuditEntry:
    """Add an entry in the caller's transaction, so it commits or rolls back with the change.

    Keep `details` to strings, integers, booleans and nulls: the hash must be reproducible
    from what the database returns.
    """
    # Send the caller's pending writes first, so they are not done while holding the lock
    # and so row locks are always taken before it.
    session.flush()
    # One writer per tenant at a time, held until the transaction ends, so every entry
    # has exactly one successor.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant, 0))"),
        {"tenant": f"audit:{tenant_id}"},
    )
    prev_hash = session.scalar(
        select(AuditEntry.hash)
        .where(AuditEntry.tenant_id == tenant_id)
        .order_by(AuditEntry.id.desc())
        .limit(1)
    )
    # The database clock, read under the lock: one clock for every writer, so timestamps
    # follow chain order.
    occurred_at = session.scalar(select(func.clock_timestamp()))
    assert occurred_at is not None  # noqa: S101 - clock_timestamp() always returns a value
    entry = AuditEntry(
        tenant_id=tenant_id,
        occurred_at=occurred_at,
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
        prev_hash=prev_hash,
        hash=entry_hash(
            prev_hash=prev_hash,
            tenant_id=tenant_id,
            occurred_at=occurred_at,
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        ),
    )
    session.add(entry)
    session.flush()
    return entry


@dataclass(frozen=True)
class ChainReport:
    consistent: bool  # the stored hashes agree with the stored contents and links
    entries: int  # entries checked
    first_bad_id: int | None = None
    reason: str | None = None


def verify_chain(session: Session, tenant_id: uuid.UUID) -> ChainReport:
    """Recompute a tenant's chain from the first entry. Stops at the first break."""
    previous: str | None = None
    checked = 0
    query = select(AuditEntry).where(AuditEntry.tenant_id == tenant_id).order_by(AuditEntry.id)
    for entry in session.scalars(query.execution_options(yield_per=500)):
        checked += 1
        if entry.prev_hash != previous:
            return ChainReport(False, checked, entry.id, "does not link to the previous entry")
        expected = entry_hash(
            prev_hash=entry.prev_hash,
            tenant_id=entry.tenant_id,
            occurred_at=entry.occurred_at,
            actor=entry.actor,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            details=entry.details,
        )
        if entry.hash != expected:
            return ChainReport(False, checked, entry.id, "hash does not match the entry's contents")
        previous = entry.hash
    return ChainReport(True, checked)
