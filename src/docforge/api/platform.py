"""The platform administrator's screens: every workspace's figures, and what happened where.

Only a platform administrator's own session reaches these; the check is made before the
owner's functions behind them are called.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from docforge.api.auth import platform_admin
from docforge.auth import Principal
from docforge.platform import PlatformService

PlatformAdmin = Annotated[Principal, Depends(platform_admin)]


class WorkspaceOut(BaseModel):
    tenant_id: uuid.UUID
    organisation: str
    kind: str  # personal or organisation
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


class OverviewOut(BaseModel):
    totals: dict[str, Any]
    workspaces: list[WorkspaceOut]


class ActivityOut(BaseModel):
    id: int
    occurred_at: datetime
    tenant_id: uuid.UUID
    organisation: str
    actor_name: str
    action: str
    action_label: str
    target_label: str


class ActivityPageOut(BaseModel):
    items: list[ActivityOut]
    next_before: int | None


def platform_router(platform: PlatformService) -> APIRouter:
    router = APIRouter(prefix="/v1/platform")

    @router.get("/overview", response_model=OverviewOut)
    def overview(principal: PlatformAdmin, response: Response) -> OverviewOut:
        """Every workspace's figures, most recently active first, and the totals. Counts and
        names only: never a document's name or contents."""
        found = platform.overview()
        response.headers["Cache-Control"] = "no-store"
        return OverviewOut(
            totals=found.totals,
            workspaces=[WorkspaceOut(**vars(w)) for w in found.workspaces],
        )

    @router.get("/activity", response_model=ActivityPageOut)
    def activity(
        principal: PlatformAdmin,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before: Annotated[int | None, Query(ge=1)] = None,
        tenant_id: uuid.UUID | None = None,
    ) -> ActivityPageOut:
        """What happened in every workspace (or one), newest first, as labels: no details."""
        items, next_before = platform.activity(before=before, limit=limit, tenant_id=tenant_id)
        response.headers["Cache-Control"] = "no-store"
        return ActivityPageOut(
            items=[ActivityOut(**vars(i)) for i in items], next_before=next_before
        )

    return router
