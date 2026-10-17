"""Workspace settings: the choices a workspace makes for itself, read where they apply.

No row is the defaults, so a workspace that never changed anything has nothing stored. A
change is audited with the old and the new value, in the change's own transaction.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from docforge import audit
from docforge.db.models import WorkspaceSettings
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.trust.invoice_rules import DEFAULT_NEAR_EXPIRY_MONTHS

MAX_NEAR_EXPIRY_MONTHS = 36


@dataclass(frozen=True)
class SettingsView:
    near_expiry_months: int
    updated_by: str | None  # the person's or key's name; None while the defaults stand
    updated_at: datetime | None


def near_expiry_months(session: Session, tenant_id: uuid.UUID) -> int:
    """The workspace's near-expiry window in months, 0 for off."""
    months = session.scalar(
        select(WorkspaceSettings.near_expiry_months).where(WorkspaceSettings.tenant_id == tenant_id)
    )
    return DEFAULT_NEAR_EXPIRY_MONTHS if months is None else months


class WorkspaceSettingsService:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @scoped
    def get(self, tenant_id: uuid.UUID) -> SettingsView:
        with self._sessions() as session:
            return _view(session.get(WorkspaceSettings, tenant_id))

    @scoped
    def update(
        self, tenant_id: uuid.UUID, *, near_expiry_months: int, actor: str, name: str
    ) -> SettingsView:
        """`actor` as the audit log names them, `name` as the screen shows who changed it
        (cut to the column's 200 characters). Raises `ValueError` for a window outside 0 to 36
        months."""
        name = name[:200]
        if not 0 <= near_expiry_months <= MAX_NEAR_EXPIRY_MONTHS:
            raise ValueError(f"the window is 0 to {MAX_NEAR_EXPIRY_MONTHS} months")
        with self._sessions.begin() as session:
            # The defaults' row first, if there is none, so two first changes at once queue
            # on its lock rather than clash on its key.
            session.execute(
                insert(WorkspaceSettings)
                .values(
                    tenant_id=tenant_id,
                    near_expiry_months=DEFAULT_NEAR_EXPIRY_MONTHS,
                    updated_by=name,
                )
                .on_conflict_do_nothing()
            )
            row = session.get(
                WorkspaceSettings, tenant_id, with_for_update=True, populate_existing=True
            )
            assert row is not None  # noqa: S101 - made just above if it was not there
            old = row.near_expiry_months
            row.near_expiry_months = near_expiry_months
            row.updated_by = name
            row.updated_at = func.now()  # the database's clock, as on insert
            audit.append(
                session,
                tenant_id=tenant_id,
                actor=actor,
                action="settings.changed",
                target_type="workspace",
                target_id=str(tenant_id),
                details={"setting": "near_expiry_months", "old": old, "new": near_expiry_months},
            )
            session.flush()
            session.refresh(row)  # updated_at as the database set it
            return _view(row)


def _view(row: WorkspaceSettings | None) -> SettingsView:
    if row is None:
        return SettingsView(DEFAULT_NEAR_EXPIRY_MONTHS, None, None)
    return SettingsView(row.near_expiry_months, row.updated_by, row.updated_at)
