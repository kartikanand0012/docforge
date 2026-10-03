"""Anchoring the audit chain outside the database.

The chain shows an entry was edited or removed, unless someone with owner rights rewrites
entries and recomputes every hash after them. To catch that, the head of each tenant's chain
(its last entry's id and hash) is copied out from time to time, to object storage the
database cannot write. In production that bucket should have S3 Object Lock (write once), so
the copies cannot be changed either. Verification then requires the chain to still contain
each anchored entry with the anchored hash.

    python -m docforge.anchors      # anchor every tenant's chain now (run it on a schedule)
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docforge import audit
from docforge.db.models import AuditEntry, Tenant
from docforge.db.tenancy import tenant_scope
from docforge.storage import ObjectNotFound, ObjectStore


@dataclass(frozen=True)
class AnchoredReport:
    consistent: bool
    entries: int
    first_bad_id: int | None
    reason: str | None
    anchors_checked: int


class AnchorStore:
    def __init__(self, store: ObjectStore, prefix: str = "audit-anchors") -> None:
        self._store = store
        self._prefix = prefix

    def _latest_key(self, tenant_id: uuid.UUID) -> str:
        return f"{self._prefix}/{tenant_id}/latest.json"

    def anchor(self, session: Session, tenant_id: uuid.UUID) -> dict[str, Any]:
        """Copy the head of the tenant's chain out of the database. Returns what was written."""
        last = session.scalars(
            select(AuditEntry)
            .where(AuditEntry.tenant_id == tenant_id)
            .order_by(AuditEntry.id.desc())
            .limit(1)
        ).first()
        now = datetime.now(UTC)
        anchor: dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "last_id": last.id if last else None,
            "last_hash": last.hash if last else None,
            "anchored_at": now.isoformat(),
        }
        body = json.dumps(anchor, sort_keys=True).encode()
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        # One object per anchor (kept), and a pointer to the newest.
        self._store.put(f"{self._prefix}/{tenant_id}/{stamp}.json", body, "application/json")
        self._store.put(self._latest_key(tenant_id), body, "application/json")
        return anchor

    def latest(self, tenant_id: uuid.UUID) -> dict[str, Any] | None:
        try:
            loaded: dict[str, Any] = json.loads(self._store.get(self._latest_key(tenant_id)))
        except ObjectNotFound:
            return None
        return loaded


def verify_with_anchors(
    session: Session, tenant_id: uuid.UUID, anchors: AnchorStore
) -> AnchoredReport:
    """The chain check, and then whether the chain still holds the latest anchored entry."""
    chain = audit.verify_chain(session, tenant_id)
    if not chain.consistent:
        return AnchoredReport(False, chain.entries, chain.first_bad_id, chain.reason, 0)
    anchor = anchors.latest(tenant_id)
    if anchor is None or anchor["last_id"] is None:
        return AnchoredReport(True, chain.entries, None, None, 0)
    entry = session.get(AuditEntry, anchor["last_id"])
    if entry is None or entry.tenant_id != tenant_id or entry.hash != anchor["last_hash"]:
        return AnchoredReport(
            False,
            chain.entries,
            anchor["last_id"],
            f"differs from the anchor taken at {anchor['anchored_at']}",
            1,
        )
    return AnchoredReport(True, chain.entries, None, None, 1)


def anchor_all(sessions: Any, anchors: AnchorStore) -> int:
    """Anchor every tenant's chain. Returns how many tenants were anchored."""
    with sessions() as session:
        tenants = list(session.scalars(select(Tenant.id)))
    for tenant_id in tenants:
        with tenant_scope(tenant_id), sessions() as session:
            anchors.anchor(session, tenant_id)
    return len(tenants)


def main() -> int:
    from docforge.config import get_settings
    from docforge.db.session import make_engine, make_session_factory
    from docforge.storage import S3ObjectStore

    settings = get_settings()
    sessions = make_session_factory(make_engine(settings.database_url.get_secret_value()))
    count = anchor_all(sessions, AnchorStore(S3ObjectStore.from_settings(settings)))
    print(f"Anchored the audit chains of {count} organisations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
