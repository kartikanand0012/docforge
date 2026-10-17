"""Workspace settings: read by anyone in the workspace, changed by an administrator or by a
personal workspace's own person (who administers nothing else, having no one to add)."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from docforge.api.auth import NOT_ALLOWED, current_principal, require
from docforge.auth import MEMBER, Principal
from docforge.workspace_settings import (
    MAX_NEAR_EXPIRY_MONTHS,
    SettingsView,
    WorkspaceSettingsService,
)

Reader = Annotated[Principal, Depends(require("documents:read"))]


async def _may_change(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if not (principal.can("admin") or principal.role == MEMBER):
        raise HTTPException(403, NOT_ALLOWED)
    return principal


Changer = Annotated[Principal, Depends(_may_change)]


class SettingsOut(BaseModel):
    # Stock with less left than this when billed is flagged on the line; 0 is off.
    near_expiry_months: int
    updated_by: str | None  # null while the defaults stand
    updated_at: datetime | None


class SettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    near_expiry_months: Annotated[int, Field(ge=0, le=MAX_NEAR_EXPIRY_MONTHS, strict=True)]


def _out(view: SettingsView) -> SettingsOut:
    return SettingsOut(
        near_expiry_months=view.near_expiry_months,
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
            actor=principal.actor,
            name=principal.name,
        )
        return _out(changed)

    return router
