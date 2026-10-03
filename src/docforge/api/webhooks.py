"""Webhook management, for administrators."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.webhooks import UnsafeDestination, WebhookService

Admin = Annotated[Principal, Depends(require("admin"))]


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=2000)
    events: list[str] = Field(min_length=1, max_length=10)


class WebhookCreated(BaseModel):
    id: uuid.UUID
    secret: str  # shown once: verify `DocForge-Signature` with it


class WebhookOut(BaseModel):
    id: uuid.UUID
    url: str
    events: list[str]
    active: bool
    created_at: datetime


class DeliveryOut(BaseModel):
    event_id: uuid.UUID
    event_type: str
    status: str
    attempts: int
    last_status: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime


def webhooks_router(hooks: WebhookService) -> APIRouter:
    router = APIRouter(prefix="/v1/webhooks")

    @router.post("", status_code=201, response_model=WebhookCreated)
    async def create(body: WebhookIn, principal: Admin) -> WebhookCreated:
        try:
            hook_id, secret = await run_in_threadpool(
                lambda: hooks.create(
                    principal.tenant_id,
                    url=body.url,
                    events=body.events,
                    created_by=principal.actor,
                )
            )
        except (UnsafeDestination, ValueError) as error:
            raise HTTPException(422, f"{str(error)[0].upper()}{str(error)[1:]}.") from error
        return WebhookCreated(id=hook_id, secret=secret)

    @router.get("", response_model=list[WebhookOut])
    def listing(principal: Admin) -> list[WebhookOut]:
        return [WebhookOut(**hook) for hook in hooks.webhooks(principal.tenant_id)]

    @router.delete("/{webhook_id}", status_code=204)
    def remove(webhook_id: uuid.UUID, principal: Admin) -> Response:
        try:
            hooks.deactivate(principal.tenant_id, webhook_id)
        except LookupError as error:
            raise HTTPException(404, "No such webhook.") from error
        return Response(status_code=204)

    @router.get("/{webhook_id}/deliveries", response_model=list[DeliveryOut])
    def deliveries(webhook_id: uuid.UUID, principal: Admin) -> list[DeliveryOut]:
        try:
            return [DeliveryOut(**row) for row in hooks.deliveries(principal.tenant_id, webhook_id)]
        except LookupError as error:
            raise HTTPException(404, "No such webhook.") from error

    @router.post("/{webhook_id}/test", status_code=202)
    def send_test(webhook_id: uuid.UUID, principal: Admin) -> dict[str, str]:
        """Queue a `webhook.test` event for this webhook."""
        try:
            event = hooks.emit_test(principal.tenant_id, webhook_id)
        except LookupError as error:
            raise HTTPException(404, "No such webhook.") from error
        return {"event_id": str(event)}

    return router
