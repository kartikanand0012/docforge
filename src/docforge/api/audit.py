"""The audit log for administrators: entries filtered and paged, their filter choices, and
the filtered view as CSV. Only reads, except that an export is itself logged."""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from docforge.api.auth import require
from docforge.audit_log import AuditLogService, Filters
from docforge.auth import Principal

Admin = Annotated[Principal, Depends(require("admin"))]


class EntryOut(BaseModel):
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


class PageOut(BaseModel):
    items: list[EntryOut]
    next_before: int | None


class ChoiceOut(BaseModel):
    value: str
    label: str


class ChoicesOut(BaseModel):
    actions: list[ChoiceOut]
    actors: list[ChoiceOut]


def _utc(moment: datetime | None) -> datetime | None:
    """An instant; one without a zone is taken as UTC."""
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def _filters(
    action: str | None = None,
    actor: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
) -> Filters:
    return Filters(action, actor, target_type, target_id, _utc(since), _utc(until))


Chosen = Annotated[Filters, Depends(_filters)]


def audit_router(log: AuditLogService) -> APIRouter:
    router = APIRouter(prefix="/v1/audit")

    @router.get("", response_model=PageOut)
    async def entries(
        principal: Admin,
        filters: Chosen,
        before: int | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> PageOut:
        """Newest first. `before` is the `next_before` of the last page."""
        page = await run_in_threadpool(
            lambda: log.entries(principal.tenant_id, filters, before=before, limit=limit)
        )
        return PageOut(
            items=[EntryOut.model_validate(vars(item)) for item in page.items],
            next_before=page.next_before,
        )

    @router.get("/filters", response_model=ChoicesOut)
    async def choices(principal: Admin) -> ChoicesOut:
        found = await run_in_threadpool(log.filters, principal.tenant_id)
        return ChoicesOut(
            actions=[ChoiceOut(value=v, label=n) for v, n in found.actions],
            actors=[ChoiceOut(value=v, label=n) for v, n in found.actors],
        )

    @router.get("/export.csv", response_class=Response)
    async def export(principal: Admin, filters: Chosen) -> Response:
        """The filtered view, newest first, at most 10,000 rows; the export is logged."""
        body, truncated = await run_in_threadpool(
            lambda: log.export(principal.tenant_id, filters, actor=principal.actor)
        )
        return Response(
            body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="docforge-audit-log.csv"',
                "X-DocForge-Truncated": "true" if truncated else "false",
                "Cache-Control": "no-store",
            },
        )

    return router
