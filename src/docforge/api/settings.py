"""Workspace settings: read by anyone in the workspace, changed by an administrator or by a
personal workspace's own person (who administers nothing else, having no one to add)."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from docforge.api.auth import NOT_ALLOWED, current_principal, require
from docforge.auth import MEMBER, Principal
from docforge.workspace_settings import (
    MAX_AMOUNT_TOLERANCE,
    MAX_NEAR_EXPIRY_MONTHS,
    MAX_QTY_TOLERANCE,
    MAX_RATE_TOLERANCE,
    SettingsView,
    WorkspaceSettingsService,
)

Reader = Annotated[Principal, Depends(require("documents:read"))]


async def _may_change(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if not (principal.can("admin") or principal.role == MEMBER):
        raise HTTPException(403, NOT_ALLOWED)
    return principal


Changer = Annotated[Principal, Depends(_may_change)]


class TolerancesOut(BaseModel):
    """How far a bill may differ from its PO and still be shown as matched, with a note."""

    rate: Decimal  # rupees per unit
    qty: int  # units
    free_qty: int  # units
    amount: Decimal  # a line's taxable amount, rupees


class SettingsOut(BaseModel):
    # Stock with less left than this when billed is flagged on the line; 0 is off.
    near_expiry_months: int
    variance_tolerances: TolerancesOut
    updated_by: str | None  # null while the defaults stand
    updated_at: datetime | None


Rupees = Annotated[Decimal, Field(ge=0, decimal_places=2)]
Units = Annotated[int, Field(ge=0, le=MAX_QTY_TOLERANCE, strict=True)]


class TolerancesChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rate: Annotated[Rupees, Field(le=MAX_RATE_TOLERANCE)] | None = None
    qty: Units | None = None
    free_qty: Units | None = None
    amount: Annotated[Rupees, Field(le=MAX_AMOUNT_TOLERANCE)] | None = None


class SettingsChange(BaseModel):
    """Any of the settings; at least one."""

    model_config = ConfigDict(extra="forbid")

    near_expiry_months: (
        Annotated[int, Field(ge=0, le=MAX_NEAR_EXPIRY_MONTHS, strict=True)] | None
    ) = None
    variance_tolerances: TolerancesChange | None = None

    @model_validator(mode="after")
    def _something(self) -> Self:
        tolerances = self.variance_tolerances
        if self.near_expiry_months is None and not (
            tolerances and tolerances.model_dump(exclude_none=True)
        ):
            raise ValueError("Change at least one setting.")
        return self


def _out(view: SettingsView) -> SettingsOut:
    t = view.tolerances
    return SettingsOut(
        near_expiry_months=view.near_expiry_months,
        variance_tolerances=TolerancesOut(
            rate=t.rate, qty=t.qty, free_qty=t.free_qty, amount=t.amount
        ),
        updated_by=view.updated_by,
        updated_at=view.updated_at,
    )


def settings_router(settings: WorkspaceSettingsService) -> APIRouter:
    router = APIRouter(prefix="/v1/settings")

    @router.get("", response_model=SettingsOut)
    def read_settings(principal: Reader) -> SettingsOut:
        return _out(settings.get(principal.tenant_id))

    @router.patch("", response_model=SettingsOut)
    def change_settings(body: SettingsChange, principal: Changer) -> SettingsOut:
        changed = settings.update(
            principal.tenant_id,
            near_expiry_months=body.near_expiry_months,
            tolerances=(
                body.variance_tolerances.model_dump(exclude_none=True)
                if body.variance_tolerances
                else None
            ),
            actor=principal.actor,
            name=principal.name,
        )
        return _out(changed)

    return router
