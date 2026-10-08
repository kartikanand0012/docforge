"""Webhook management, for administrators: make, list, disable and enable, delete, rotate
the secret, send a test, and see and re-send deliveries. Every change is audited."""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.limits import Limits
from docforge.webhooks import (
    EVENTS,
    DeliveryNotFailed,
    DeliveryNotFound,
    TooManyWebhooks,
    UnsafeDestination,
    WebhookDisabled,
    WebhookService,
)

Admin = Annotated[Principal, Depends(require("admin"))]
logger = logging.getLogger(__name__)
SENDS_PER_MINUTE = 10  # test sends and re-sends, per organisation: not a way to flood anyone
CHANGES_PER_MINUTE = 20  # making and enabling: each resolves a name, which must not be a probe
_NOT_FOUND = "No such webhook."


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=2000)
    events: list[str] = Field(min_length=1, max_length=10)


class ActiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool


class WebhookCreated(BaseModel):
    id: uuid.UUID
    secret: str  # shown once: verify `DocForge-Signature` with it


class SecretOut(BaseModel):
    secret: str  # shown once


class LastDeliveryOut(BaseModel):
    status: str
    last_status: int | None
    created_at: datetime


class WebhookOut(BaseModel):
    id: uuid.UUID
    url: str
    events: list[str]
    active: bool
    created_at: datetime
    created_by: str
    secret_rotated_at: datetime | None
    last_delivery: LastDeliveryOut | None


class DeliveryOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    status: str
    attempts: int
    last_status: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime
    last_attempt_at: datetime | None
    next_attempt_at: datetime | None


def _sentence(error: Exception) -> str:
    text = str(error) or "that is not allowed"
    return f"{text[0].upper()}{text[1:]}."


def _unsafe(error: UnsafeDestination) -> HTTPException:
    """The same answer whatever the name resolved to: an administrator's request must not map
    the server's network. The reason is logged for the operator."""
    logger.info("webhook destination refused: %s", error)
    return HTTPException(
        422, "That address is not allowed: a webhook must use https and a public address."
    )


def webhooks_router(hooks: WebhookService, limits: Limits) -> APIRouter:
    router = APIRouter(prefix="/v1/webhooks")

    def take(principal: Principal) -> None:
        if not limits.allow(f"webhook-send:{principal.tenant_id}", SENDS_PER_MINUTE):
            raise HTTPException(429, "Too many sends this minute; wait a minute.")

    def change(principal: Principal) -> None:
        if not limits.allow(f"webhook-change:{principal.tenant_id}", CHANGES_PER_MINUTE):
            raise HTTPException(429, "Too many changes this minute; wait a minute.")

    @router.post("", status_code=201, response_model=WebhookCreated)
    async def create(body: WebhookIn, principal: Admin, response: Response) -> WebhookCreated:
        await run_in_threadpool(change, principal)
        try:
            hook_id, secret = await run_in_threadpool(
                lambda: hooks.create(
                    principal.tenant_id,
                    url=body.url,
                    events=body.events,
                    created_by=principal.actor,
                )
            )
        except TooManyWebhooks as error:
            raise HTTPException(409, _sentence(error)) from error
        except UnsafeDestination as error:
            raise _unsafe(error) from error
        except ValueError as error:
            raise HTTPException(422, _sentence(error)) from error
        response.headers["Cache-Control"] = "no-store"
        return WebhookCreated(id=hook_id, secret=secret)

    @router.get("", response_model=list[WebhookOut])
    def listing(principal: Admin) -> list[WebhookOut]:
        return [WebhookOut(**hook) for hook in hooks.webhooks(principal.tenant_id)]

    @router.get("/events", response_model=list[str])
    def events(principal: Admin) -> list[str]:
        """What a webhook may subscribe to."""
        return list(EVENTS)

    @router.patch("/{webhook_id}", response_model=dict[str, bool])
    def set_active(webhook_id: uuid.UUID, body: ActiveIn, principal: Admin) -> dict[str, bool]:
        """Disable (stop sending, keep it) or enable again (the destination is re-checked)."""
        change(principal)
        try:
            hooks.set_active(principal.tenant_id, webhook_id, body.active, actor=principal.actor)
        except LookupError as error:
            raise HTTPException(404, _NOT_FOUND) from error
        except UnsafeDestination as error:
            raise _unsafe(error) from error
        return {"active": body.active}

    @router.delete("/{webhook_id}", status_code=204)
    def remove(webhook_id: uuid.UUID, principal: Admin) -> Response:
        """Stops for good: out of the list, its deliveries kept as a record."""
        try:
            hooks.delete(principal.tenant_id, webhook_id, actor=principal.actor)
        except LookupError as error:
            raise HTTPException(404, _NOT_FOUND) from error
        return Response(status_code=204)

    @router.post("/{webhook_id}/secret", response_model=SecretOut)
    def rotate(webhook_id: uuid.UUID, principal: Admin, response: Response) -> SecretOut:
        """A new signing secret, shown once; the old one stops verifying at once."""
        try:
            secret = hooks.rotate(principal.tenant_id, webhook_id, actor=principal.actor)
        except LookupError as error:
            raise HTTPException(404, _NOT_FOUND) from error
        response.headers["Cache-Control"] = "no-store"
        return SecretOut(secret=secret)

    @router.get("/{webhook_id}/deliveries", response_model=list[DeliveryOut])
    def deliveries(
        webhook_id: uuid.UUID,
        principal: Admin,
        before: uuid.UUID | None = None,
        status: Literal["pending", "delivered", "failed"] | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[DeliveryOut]:
        """Newest first; for the next page, `before` is the last one's id."""
        try:
            page = hooks.deliveries(
                principal.tenant_id, webhook_id, before=before, status=status, limit=limit
            )
        except DeliveryNotFound as error:
            raise HTTPException(404, "No such delivery.") from error
        except LookupError as error:
            raise HTTPException(404, _NOT_FOUND) from error
        return [DeliveryOut(**row) for row in page.items]

    @router.post("/{webhook_id}/test", status_code=202)
    def send_test(webhook_id: uuid.UUID, principal: Admin) -> dict[str, str]:
        """Queue a `webhook.test` event for this webhook."""
        take(principal)
        try:
            event = hooks.emit_test(principal.tenant_id, webhook_id, actor=principal.actor)
        except LookupError as error:
            raise HTTPException(404, _NOT_FOUND) from error
        except WebhookDisabled as error:
            raise HTTPException(409, "The webhook is disabled. Enable it first.") from error
        except UnsafeDestination as error:
            raise _unsafe(error) from error
        return {"event_id": str(event)}

    @router.post("/{webhook_id}/deliveries/{delivery_id}/resend", status_code=202)
    def resend(webhook_id: uuid.UUID, delivery_id: uuid.UUID, principal: Admin) -> dict[str, Any]:
        """Send a failed delivery again, with its own event id and fresh attempts."""
        take(principal)
        try:
            event = hooks.resend(
                principal.tenant_id, webhook_id, delivery_id, actor=principal.actor
            )
        except DeliveryNotFound as error:
            raise HTTPException(404, "No such delivery.") from error
        except LookupError as error:
            raise HTTPException(404, _NOT_FOUND) from error
        except DeliveryNotFailed as error:
            raise HTTPException(409, "Only a failed delivery is sent again.") from error
        except WebhookDisabled as error:
            raise HTTPException(409, "The webhook is disabled. Enable it first.") from error
        except UnsafeDestination as error:
            raise _unsafe(error) from error
        return {"event_id": str(event)}

    return router
