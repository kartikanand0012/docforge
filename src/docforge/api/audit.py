"""The audit log for administrators: entries filtered and paged, their filter choices, and
the filtered view as CSV. Only reads, except that an export is itself logged."""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from docforge.api.auth import require
from docforge.audit_log import AuditLogService, Filters
from docforge.auth import Principal
from docforge.limits import Limits

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


_TEXT = Query(max_length=200, pattern=r"^[^\x00]*$")  # no NUL: Postgres refuses it


def _filters(
    action: Annotated[str | None, _TEXT] = None,
    actor: Annotated[str | None, _TEXT] = None,
    target_type: Annotated[str | None, _TEXT] = None,
    target_id: Annotated[str | None, _TEXT] = None,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
) -> Filters:
    return Filters(action, actor, target_type, target_id, _utc(since), _utc(until))


Chosen = Annotated[Filters, Depends(_filters)]


LISTS_PER_MINUTE = 60
EXPORTS_PER_MINUTE = 6  # each reads up to 10,000 entries, and is itself logged


def audit_router(log: AuditLogService, limits: Limits) -> APIRouter:
    router = APIRouter(prefix="/v1/audit")

    def take(principal: Principal, kind: str, per_minute: int) -> None:
        if not limits.allow(f"audit-{kind}:{principal.tenant_id}", per_minute):
            raise HTTPException(429, "Too many requests this minute; wait a minute.")

    @router.get("", response_model=PageOut)
    async def entries(
        principal: Admin,
        filters: Chosen,
        before: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> PageOut:
        """Newest first. `before` is the `next_before` of the last page."""
        await run_in_threadpool(take, principal, "list", LISTS_PER_MINUTE)
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
        await run_in_threadpool(take, principal, "export", EXPORTS_PER_MINUTE)
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
