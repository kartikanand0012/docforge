"""The audit log as administrators read it: filtered, paged newest first, with names.

The log keeps ids only. Names are looked up when it is read - a reviewer's name, a key's name
and prefix, a document's filename - so they stay erasable. Only the details each action allows
are shown (`audit_actions`). Reading never changes the log; an export is itself logged.
"""

import builtins
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from docforge import audit
from docforge.audit_actions import ACTIONS, label, shown
from docforge.csvsafe import to_csv
from docforge.db.models import ApiKey, AuditEntry, Document, Reviewer
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped

SYSTEM_ACTORS = {
    "system:worker": "DocForge (processing)",
    "api": "Sign-in",
    "cli": "Command line",
}
EXPORT_LIMIT = 10_000
EXPORT_COLUMNS = (
    "id", "occurred_at", "actor", "actor_name", "action", "target_type", "target_id",
    "details", "prev_hash", "hash",
)  # fmt: skip


@dataclass(frozen=True)
class Filters:
    action: str | None = None
    actor: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    since: datetime | None = None  # inclusive
    until: datetime | None = None  # exclusive

    @property
    def any(self) -> bool:
        return any(value is not None for value in vars(self).values())


@dataclass(frozen=True)
class Entry:
    id: int
    occurred_at: datetime
    actor: str
    actor_name: str
    action: str
    action_label: str
    target_type: str
    target_id: str
    target_label: str | None
    details: dict[str, Any]
    hidden_details: int
    prev_hash: str | None = None
    hash: str = ""


@dataclass(frozen=True)
class Page:
    items: list[Entry]
    next_before: int | None


@dataclass(frozen=True)
class Choices:
    actions: list[tuple[str, str]]  # (action, label)
    actors: list[tuple[str, str]] = field(default_factory=list)  # (actor, name)


class AuditLogService:
    def __init__(self, sessions: SessionFactory, *, export_limit: int = EXPORT_LIMIT) -> None:
        self._sessions = sessions
        self._export_limit = export_limit

    @scoped
    def entries(
        self,
        tenant_id: uuid.UUID,
        filters: Filters,
        *,
        before: int | None = None,
        limit: int = 50,
    ) -> Page:
        """Newest first; `before` is the last id already shown, so a page never repeats or
        skips an entry, whatever is appended meanwhile."""
        with self._sessions.begin() as session:
            rows = self._query(session, tenant_id, filters, before, limit + 1)
            items = self._named(session, tenant_id, rows[:limit])
        return Page(items, items[-1].id if len(rows) > limit else None)

    @scoped
    def filters(self, tenant_id: uuid.UUID) -> Choices:
        """What may be chosen: the registered actions, and the people, keys and system that
        can act - read from who exists, not by scanning the log."""
        actions = sorted(((name, a.label) for name, a in ACTIONS.items()), key=lambda p: p[1])
        with self._sessions() as session:
            people = session.execute(
                select(Reviewer.id, Reviewer.name)
                .where(Reviewer.tenant_id == tenant_id)
                .order_by(Reviewer.name)
            ).all()
            keys = session.execute(
                select(ApiKey.id, ApiKey.name, ApiKey.prefix)
                .where(ApiKey.tenant_id == tenant_id)
                .order_by(ApiKey.name)
            ).all()
        actors = [(f"reviewer:{i}", name) for i, name in people]
        actors += [(f"key:{i}", f"{name} ({prefix})") for i, name, prefix in keys]
        actors += sorted(SYSTEM_ACTORS.items(), key=lambda p: p[1])
        return Choices(actions, actors)

    @scoped
    def export(self, tenant_id: uuid.UUID, filters: Filters, *, actor: str) -> tuple[str, bool]:
        """The filtered view as CSV, newest first, at most `export_limit` rows; whether it
        was cut. The export is appended to the log. No filenames: a copy cannot be erased."""
        with self._sessions.begin() as session:
            rows = self._query(session, tenant_id, filters, None, self._export_limit + 1)
            truncated = len(rows) > self._export_limit
            items = self._named(session, tenant_id, rows[: self._export_limit])
            audit.append(
                session,
                tenant_id=tenant_id,
                actor=actor,
                action="audit.exported",
                target_type="audit_log",
                target_id=str(tenant_id),
                details={"rows": len(items), "truncated": truncated, "filtered": filters.any},
            )
        csv_rows = [
            {
                "id": e.id,
                "occurred_at": e.occurred_at.isoformat(),
                "actor": e.actor,
                "actor_name": e.actor_name,
                "action": e.action,
                "target_type": e.target_type,
                "target_id": e.target_id,
                "details": json.dumps(e.details, sort_keys=True, separators=(",", ":")),
                "prev_hash": e.prev_hash or "",
                "hash": e.hash,
            }
            for e in items
        ]
        body = to_csv(csv_rows) if csv_rows else ",".join(EXPORT_COLUMNS) + "\n"
        return body, truncated

    # Helpers

    @staticmethod
    def _query(
        session: Session,
        tenant_id: uuid.UUID,
        filters: Filters,
        before: int | None,
        limit: int,
    ) -> builtins.list[AuditEntry]:
        # A pathological filter gives up rather than holding a connection.
        session.execute(text("SET LOCAL statement_timeout = '5s'"))
        query = select(AuditEntry).where(AuditEntry.tenant_id == tenant_id)
        if filters.action is not None:
            query = query.where(AuditEntry.action == filters.action)
        if filters.actor is not None:
            query = query.where(AuditEntry.actor == filters.actor)
        if filters.target_type is not None:
            query = query.where(AuditEntry.target_type == filters.target_type)
        if filters.target_id is not None:
            query = query.where(AuditEntry.target_id == filters.target_id)
        if filters.since is not None:
            query = query.where(AuditEntry.occurred_at >= filters.since)
        if filters.until is not None:
            query = query.where(AuditEntry.occurred_at < filters.until)
        if before is not None:
            query = query.where(AuditEntry.id < before)
        return list(session.scalars(query.order_by(AuditEntry.id.desc()).limit(limit)))

    @staticmethod
    def _named(
        session: Session, tenant_id: uuid.UUID, rows: builtins.list[AuditEntry]
    ) -> builtins.list[Entry]:
        """Each entry with its actor's and target's names, looked up now."""
        reviewers, keys, documents = set(), set(), set()
        for row in rows:
            kind, _, rest = row.actor.partition(":")
            if kind == "reviewer" and (found := _uuid(rest)):
                reviewers.add(found)
            if kind == "key" and (found := _uuid(rest)):
                keys.add(found)
            if row.target_type == "document" and (found := _uuid(row.target_id)):
                documents.add(found)
        names: dict[str, str] = dict(SYSTEM_ACTORS)
        if reviewers:
            for i, name in session.execute(
                select(Reviewer.id, Reviewer.name).where(
                    Reviewer.tenant_id == tenant_id, Reviewer.id.in_(reviewers)
                )
            ):
                names[f"reviewer:{i}"] = name
        if keys:
            for i, name, prefix in session.execute(
                select(ApiKey.id, ApiKey.name, ApiKey.prefix).where(
                    ApiKey.tenant_id == tenant_id, ApiKey.id.in_(keys)
                )
            ):
                names[f"key:{i}"] = f"{name} ({prefix})"
        filenames: dict[str, str] = {}
        if documents:
            filenames = {
                str(i): name
                for i, name in session.execute(
                    select(Document.id, Document.filename).where(
                        Document.tenant_id == tenant_id, Document.id.in_(documents)
                    )
                )
            }
        out = []
        for row in rows:
            visible, hidden = shown(row.action, row.details or {})
            out.append(
                Entry(
                    id=row.id,
                    occurred_at=row.occurred_at,
                    actor=row.actor,
                    actor_name=names.get(row.actor, row.actor),
                    action=row.action,
                    action_label=label(row.action),
                    target_type=row.target_type,
                    target_id=row.target_id,
                    target_label=filenames.get(row.target_id)
                    if row.target_type == "document"
                    else None,
                    details=visible,
                    hidden_details=hidden,
                    prev_hash=row.prev_hash,
                    hash=row.hash,
                )
            )
        return out


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:  # an actor or target written before ids were uuids
        return None
