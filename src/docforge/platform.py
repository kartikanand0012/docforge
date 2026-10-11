"""The platform administrator's figures and activity across every workspace.

Read through the owner's functions (migration 0028), which return counts, token sums and
labels only, over the platform's own database login (PLATFORM_DATABASE_URL, migration 0030):
the application's login cannot call them. Callers must have checked that the person asking
is a platform administrator.
"""

import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from docforge.audit_actions import label
from docforge.db.session import SessionFactory
from docforge.telemetry import Prices, document_cost

SYSTEM_ACTORS = {"system:worker": "DocForge (processing)", "api": "Sign-in", "cli": "Command line"}
TARGETS = {
    "document": "Document",
    "collection": "Knowledge base",
    "api_key": "API key",
    "webhook": "Webhook",
    "reviewer": "Person",
    "tenant": "Workspace",
    "audit_log": "Audit log",
}
MAX_ACTIVITY = 200
MAX_WORKSPACES = 500  # a page of the overview, at most
CACHE_SECONDS = 30.0


def _checked(session: Session) -> None:
    """Tell the owner's functions, for this transaction only, that the caller was checked:
    without it they return nothing."""
    session.execute(text("SELECT set_config('docforge.platform_admin', 'checked', true)"))


@dataclass(frozen=True)
class Workspace:
    tenant_id: uuid.UUID
    organisation: str
    kind: str
    owner_name: str | None
    owner_email: str | None
    created_at: datetime
    last_active_at: datetime | None
    documents: int
    pages: int
    signed: int
    questions: int
    model_cost_usd: float | None
    sign_ins: int


@dataclass(frozen=True)
class Overview:
    totals: dict[str, Any]
    workspaces: list[Workspace]
    next_offset: int | None = None  # where the next page starts; None on the last


@dataclass(frozen=True)
class Activity:
    id: int
    occurred_at: datetime
    tenant_id: uuid.UUID
    organisation: str
    actor_name: str
    action: str
    action_label: str
    target_label: str


def _cost(usages: list[tuple[str, str, int, int, int]], prices: Prices) -> float | None:
    """Nothing spent is no cost; otherwise as a document's cost (None if any model has no
    price)."""
    return document_cost(usages, prices) if usages else (0.0 if prices else None)


class PlatformService:
    def __init__(
        self,
        sessions: SessionFactory,
        prices: Prices | None = None,
        *,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self._sessions = sessions
        self._prices = prices or {}
        # The overview reads every workspace: kept for a short while, per page, in this
        # process, so a dashboard left open does not read them all on every refresh.
        self._cache_seconds = cache_seconds
        self._cache: dict[tuple[int, int], tuple[float, Overview]] = {}
        self._cache_lock = threading.Lock()

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def overview(self, *, limit: int = 100, offset: int = 0) -> Overview:
        """One page of the workspaces' figures, most recently active first, and the totals
        over every workspace."""
        limit, offset = max(1, min(limit, MAX_WORKSPACES)), max(0, offset)
        now = time.monotonic()
        with self._cache_lock:
            kept = self._cache.get((limit, offset))
            if kept is not None and now - kept[0] < self._cache_seconds:
                return kept[1]
        found = self._overview(limit, offset)
        with self._cache_lock:
            self._cache[(limit, offset)] = (now, found)
        return found

    def _overview(self, limit: int, offset: int) -> Overview:
        with self._sessions.begin() as session:
            _checked(session)
            rows = session.execute(select(func.docforge_platform_workspaces(
                limit + 1, offset
            ).table_valued(
                "tenant_id", "organisation", "kind", "created_at", "owner_name", "owner_email",
                "last_active_at", "documents", "documents_last_7_days", "pages", "signed",
                "questions", "sign_ins",
            ))).all()  # fmt: skip
            totals = session.execute(select(func.docforge_platform_totals().table_valued(
                "workspaces", "documents", "documents_last_7_days", "pages", "signed",
                "questions",
            ))).one()  # fmt: skip
            usage = session.execute(select(func.docforge_platform_usage().table_valued(
                "tenant_id", "provider", "model", "input_tokens", "output_tokens",
                "thinking_tokens",
            ))).all()  # fmt: skip
            accounts, signups = session.execute(
                select(
                    func.docforge_platform_accounts().table_valued(
                        "accounts", "signups_last_7_days"
                    )
                )
            ).one()
        spent: dict[uuid.UUID, list[tuple[str, str, int, int, int]]] = defaultdict(list)
        for tenant, provider, model, i, o, t in usage:
            spent[tenant].append((provider, model, int(i), int(o), int(t)))
        workspaces = [
            Workspace(
                tenant_id=row.tenant_id,
                organisation=row.organisation,
                kind=row.kind,
                owner_name=row.owner_name,
                owner_email=row.owner_email,
                created_at=row.created_at,
                last_active_at=row.last_active_at,
                documents=int(row.documents),
                pages=int(row.pages),
                signed=int(row.signed),
                questions=int(row.questions),
                model_cost_usd=_cost(spent.get(row.tenant_id, []), self._prices),
                sign_ins=int(row.sign_ins),
            )
            for row in rows[:limit]
        ]
        everything = [usage for usages in spent.values() for usage in usages]
        cost = _cost(everything, self._prices)
        summed = {
            "workspaces": int(totals.workspaces),
            "accounts": int(accounts),
            "documents": int(totals.documents),
            "pages": int(totals.pages),
            "signed": int(totals.signed),
            "questions": int(totals.questions),
            "model_cost_usd": None if cost is None else round(cost, 6),
            "documents_last_7_days": int(totals.documents_last_7_days),
            "signups_last_7_days": int(signups),
        }
        return Overview(summed, workspaces, offset + limit if len(rows) > limit else None)

    def activity(
        self, *, before: int | None = None, limit: int = 50, tenant_id: uuid.UUID | None = None
    ) -> tuple[list[Activity], int | None]:
        """Audit entries across every workspace, newest first, as labels; and the `before`
        for the next page (None at the end)."""
        limit = max(1, min(limit, MAX_ACTIVITY))
        with self._sessions.begin() as session:
            _checked(session)
            rows = session.execute(
                select(
                    func.docforge_platform_activity(before, limit + 1, tenant_id).table_valued(
                        "id", "occurred_at", "tenant_id", "organisation", "actor", "actor_name",
                        "action", "target_type",
                    )
                )
            ).all()  # fmt: skip
        items = [
            Activity(
                id=int(row.id),
                occurred_at=row.occurred_at,
                tenant_id=row.tenant_id,
                organisation=row.organisation,
                actor_name=row.actor_name or SYSTEM_ACTORS.get(row.actor, "Someone"),
                action=row.action,
                action_label=label(row.action),
                target_label=TARGETS.get(row.target_type, "Something"),
            )
            for row in rows[:limit]
        ]
        return items, items[-1].id if len(rows) > limit else None
