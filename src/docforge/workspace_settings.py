"""Workspace settings: the choices a workspace makes for itself, read where they apply.

No row is the defaults, so a workspace that never changed anything has nothing stored. A
change is audited with the old and the new value, in the change's own transaction.
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from docforge import audit
from docforge.db.models import WorkspaceSettings
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.trust.invoice_rules import DEFAULT_NEAR_EXPIRY_MONTHS
from docforge.trust.variance import Tolerances

MAX_NEAR_EXPIRY_MONTHS = 36
# The variance tolerances' bounds, as the table's checks hold them.
MAX_RATE_TOLERANCE = Decimal(1000)
MAX_QTY_TOLERANCE = 100_000
MAX_AMOUNT_TOLERANCE = Decimal(100_000)
# Each tolerance's column, by the name the API and the audit log give it.
_TOLERANCE_COLUMNS = {
    "rate": "variance_rate_tolerance",
    "qty": "variance_qty_tolerance",
    "free_qty": "variance_free_qty_tolerance",
    "amount": "variance_amount_tolerance",
}


@dataclass(frozen=True)
class SettingsView:
    near_expiry_months: int
    updated_by: str | None  # the person's or key's name; None while the defaults stand
    updated_at: datetime | None
    tolerances: Tolerances = field(default_factory=Tolerances)


def variance_tolerances(session: Session, tenant_id: uuid.UUID) -> Tolerances:
    """How far a bill may differ from its PO and still be matched, in this workspace."""
    return _tolerances(session.get(WorkspaceSettings, tenant_id))


def _tolerances(row: WorkspaceSettings | None) -> Tolerances:
    if row is None:
        return Tolerances()
    return Tolerances(
        rate=row.variance_rate_tolerance,
        qty=row.variance_qty_tolerance,
        free_qty=row.variance_free_qty_tolerance,
        amount=row.variance_amount_tolerance,
    )


def _checked(tolerances: Tolerances) -> Tolerances:
    """Raises `ValueError` for a tolerance below zero, above its bound, or finer than a paisa."""
    for name, value, most in (
        ("rate", tolerances.rate, MAX_RATE_TOLERANCE),
        ("amount", tolerances.amount, MAX_AMOUNT_TOLERANCE),
    ):
        if not 0 <= value <= most or value != value.quantize(Decimal("0.01")):
            raise ValueError(f"the {name} tolerance is 0 to {most} rupees, to the paisa")
    for name, count in (("qty", tolerances.qty), ("free_qty", tolerances.free_qty)):
        if not 0 <= count <= MAX_QTY_TOLERANCE:
            raise ValueError(f"the {name} tolerance is 0 to {MAX_QTY_TOLERANCE:,} units")
    return tolerances


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
        self,
        tenant_id: uuid.UUID,
        *,
        near_expiry_months: int | None = None,
        tolerances: dict[str, Any] | None = None,
        actor: str,
        name: str,
    ) -> SettingsView:
        """Change the near-expiry window, any of the variance tolerances (by name: rate, qty,
        free_qty, amount), or both. `actor` as the audit log names them, `name` as the screen
        shows who changed it (cut to the column's 200 characters). Each setting that changes is
        audited with its old and new value. Raises `ValueError` for a window outside 0 to 36
        months or a tolerance out of its bounds."""
        name = name[:200]
        if near_expiry_months is not None and not 0 <= near_expiry_months <= MAX_NEAR_EXPIRY_MONTHS:
            raise ValueError(f"the window is 0 to {MAX_NEAR_EXPIRY_MONTHS} months")
        unknown = set(tolerances or {}) - set(_TOLERANCE_COLUMNS)
        if unknown:
            raise ValueError(f"no tolerance is called {sorted(unknown)[0]}")
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
            changes: list[tuple[str, Any, Any]] = []
            if near_expiry_months is not None:
                changes.append(("near_expiry_months", row.near_expiry_months, near_expiry_months))
                row.near_expiry_months = near_expiry_months
            if tolerances:
                old = _tolerances(row)
                new = _checked(replace(old, **tolerances))
                for key, column in _TOLERANCE_COLUMNS.items():
                    if key in tolerances:
                        changes.append((column, getattr(old, key), getattr(new, key)))
                        setattr(row, column, getattr(new, key))
            row.updated_by = name
            row.updated_at = func.now()  # the database's clock, as on insert
            for setting, before, after in changes:
                audit.append(
                    session,
                    tenant_id=tenant_id,
                    actor=actor,
                    action="settings.changed",
                    target_type="workspace",
                    target_id=str(tenant_id),
                    details={"setting": setting, "old": _plain(before), "new": _plain(after)},
                )
            session.flush()
            session.refresh(row)  # updated_at as the database set it
            return _view(row)


def _plain(value: Any) -> Any:
    """A setting's value as the audit log's JSON keeps it: rupees as text, exactly."""
    return str(value) if isinstance(value, Decimal) else value


def _view(row: WorkspaceSettings | None) -> SettingsView:
    if row is None:
        return SettingsView(DEFAULT_NEAR_EXPIRY_MONTHS, None, None)
    return SettingsView(row.near_expiry_months, row.updated_by, row.updated_at, _tolerances(row))
