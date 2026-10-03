"""Webhooks: events sent to an organisation's own systems, signed, retried, never doubled.

An event is written to `webhook_deliveries` in the same transaction as the change that
caused it, with one row per subscribed webhook, and a delivery job is queued in that same
transaction. Delivery POSTs the event as JSON with:

- `DocForge-Event-Id`: the event's id, the same on every attempt, so a receiver can ignore
  a repeat;
- `DocForge-Signature: t=<unix time>,v1=<hex>`: HMAC-SHA256 of `"<t>." + body` with the
  webhook's secret.

A 2xx answer is delivered; anything else is retried with the same id until the attempts
run out. A destination must be HTTPS and must resolve only to public addresses, checked when
the webhook is made and again before every attempt; redirects are not followed.
"""

import hashlib
import hmac
import ipaddress
import json
import socket
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from docforge.db.models import Webhook, WebhookDelivery
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped, tenant_scope

EVENTS = ("document.processed", "review.signed", "webhook.test")
Defer = Callable[[Session, uuid.UUID, uuid.UUID], None]  # (session, delivery id, tenant id)
Outcome = Literal["delivered", "retry", "failed", "skipped"]
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_ERROR_LIMIT = 500  # characters of an error kept, so a receiver cannot fill the table


class UnsafeDestination(ValueError):
    """The URL is not HTTPS, or its host resolves to an address that is not public."""


def check_destination(url: str, *, allow_http: bool = False, allow_private: bool = False) -> None:
    """Raise `UnsafeDestination` unless `url` may receive webhooks."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise UnsafeDestination("the URL cannot be read") from error
    if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
        raise UnsafeDestination("a webhook URL must use https")
    if not parts.hostname or parts.username or parts.password:
        raise UnsafeDestination("a webhook URL needs a host and no user name or password")
    try:
        found = socket.getaddrinfo(
            parts.hostname,
            port or (443 if parts.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise UnsafeDestination(f"{parts.hostname} cannot be resolved") from error
    for *_, address in found:
        ip = ipaddress.ip_address(address[0])
        if not allow_private and not ip.is_global:
            raise UnsafeDestination(
                f"{parts.hostname} resolves to {ip}, which is not a public address"
            )


def derive_secret(key: bytes, webhook_id: uuid.UUID, version: int) -> str:
    """The webhook's signing secret: from the server key, so it is never stored."""
    digest = hmac.new(key, f"webhook:{webhook_id}:{version}".encode(), hashlib.sha256).hexdigest()
    return f"whsec_{digest}"


def sign(secret: str, body: bytes, *, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


class WebhookService:
    def __init__(
        self,
        sessions: SessionFactory,
        signing_key: bytes,
        defer: Defer,
        *,
        allow_http: bool = False,
        allow_private: bool = False,
        max_attempts: int = 8,
        client: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._sessions = sessions
        self._key = signing_key
        self._defer = defer
        self._allow_http = allow_http
        self._allow_private = allow_private
        self.max_attempts = max_attempts
        self._client = client or (lambda: httpx.Client(timeout=_TIMEOUT, follow_redirects=False))

    def _check(self, url: str) -> None:
        check_destination(url, allow_http=self._allow_http, allow_private=self._allow_private)

    # Managing

    @scoped
    def create(
        self, tenant_id: uuid.UUID, *, url: str, events: Sequence[str], created_by: str = "admin"
    ) -> tuple[uuid.UUID, str]:
        """A new webhook and its signing secret, which is shown only now."""
        unknown = sorted(set(events) - set(EVENTS))
        if not events or unknown:
            raise ValueError(f"events must be some of {', '.join(EVENTS)}")
        self._check(url)
        with self._sessions.begin() as session:
            webhook = Webhook(
                tenant_id=tenant_id, url=url, events=sorted(set(events)), created_by=created_by
            )
            session.add(webhook)
            session.flush()
            return webhook.id, derive_secret(self._key, webhook.id, webhook.secret_version)

    @scoped
    def webhooks(self, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
        with self._sessions() as session:
            return [
                {
                    "id": hook.id,
                    "url": hook.url,
                    "events": list(hook.events),
                    "active": hook.active,
                    "created_at": hook.created_at,
                }
                for hook in session.scalars(
                    select(Webhook)
                    .where(Webhook.tenant_id == tenant_id)
                    .order_by(Webhook.created_at)
                )
            ]

    @scoped
    def deactivate(self, tenant_id: uuid.UUID, webhook_id: uuid.UUID) -> None:
        with self._sessions.begin() as session:
            hook = session.scalar(
                select(Webhook).where(Webhook.tenant_id == tenant_id, Webhook.id == webhook_id)
            )
            if hook is None:
                raise LookupError("no such webhook")
            hook.active = False

    @scoped
    def deliveries(
        self, tenant_id: uuid.UUID, webhook_id: uuid.UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._sessions() as session:
            if (
                session.scalar(
                    select(Webhook.id).where(
                        Webhook.tenant_id == tenant_id, Webhook.id == webhook_id
                    )
                )
                is None
            ):
                raise LookupError("no such webhook")
            rows = session.scalars(
                select(WebhookDelivery)
                .where(WebhookDelivery.webhook_id == webhook_id)
                .order_by(WebhookDelivery.created_at.desc())
                .limit(limit)
            )
            return [
                {
                    "event_id": row.event_id,
                    "event_type": row.event_type,
                    "status": row.status,
                    "attempts": row.attempts,
                    "last_status": row.last_status,
                    "last_error": row.last_error,
                    "delivered_at": row.delivered_at,
                    "created_at": row.created_at,
                }
                for row in rows
            ]

    # Emitting and delivering

    def emit(
        self,
        session: Session,
        tenant_id: uuid.UUID,
        event_type: str,
        data: dict[str, Any],
        *,
        event_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Record `event_type` for every active subscribed webhook, in `session`'s transaction.

        The same `event_id` emitted again adds nothing.
        """
        event_id = event_id or uuid.uuid4()
        payload = {
            "id": str(event_id),
            "type": event_type,
            "created_at": datetime.now(UTC).isoformat(),
            "data": data,
        }
        hooks = session.scalars(
            select(Webhook.id).where(
                Webhook.tenant_id == tenant_id,
                Webhook.active.is_(True),
                Webhook.events.any(event_type),  # type: ignore[arg-type]
            )
        ).all()
        for hook_id in hooks:
            delivery_id = session.execute(
                insert(WebhookDelivery)
                .values(
                    tenant_id=tenant_id,
                    webhook_id=hook_id,
                    event_id=event_id,
                    event_type=event_type,
                    payload=payload,
                )
                .on_conflict_do_nothing(index_elements=["webhook_id", "event_id"])
                .returning(WebhookDelivery.id)
            ).scalar_one_or_none()
            if delivery_id is not None:
                self._defer(session, delivery_id, tenant_id)
        return event_id

    @scoped
    def emit_test(self, tenant_id: uuid.UUID) -> uuid.UUID:
        """A `webhook.test` event for the tenant's webhooks that subscribe to it."""
        with self._sessions.begin() as session:
            return self.emit(session, tenant_id, "webhook.test", {"message": "test from DocForge"})

    def deliver(self, delivery_id: uuid.UUID, tenant_id: uuid.UUID) -> Outcome:
        """One attempt. `retry` means the queue should call again later."""
        with tenant_scope(tenant_id), self._sessions.begin() as session:
            # Locked for the attempt, so two workers never send the same delivery at once.
            row = session.execute(
                select(WebhookDelivery, Webhook)
                .join(Webhook, Webhook.id == WebhookDelivery.webhook_id)
                .where(WebhookDelivery.id == delivery_id)
                .with_for_update(of=WebhookDelivery, skip_locked=True)
            ).first()
            if row is None:
                return "skipped"
            delivery, hook = row
            if delivery.status != "pending" or not hook.active:
                return "skipped"
            delivery.attempts += 1
            status, error = self._send(hook, delivery)
            delivery.last_status, delivery.last_error = status, error
            if status is not None and 200 <= status < 300:
                delivery.status, delivery.delivered_at = "delivered", datetime.now(UTC)
                return "delivered"
            if delivery.attempts >= self.max_attempts:
                delivery.status = "failed"
                return "failed"
            return "retry"

    def _send(self, hook: Webhook, delivery: WebhookDelivery) -> tuple[int | None, str | None]:
        try:
            self._check(hook.url)
        except UnsafeDestination as error:
            return None, str(error)[:_ERROR_LIMIT]
        body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
        secret = derive_secret(self._key, hook.id, hook.secret_version)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "DocForge-Webhooks/1",
            "DocForge-Event-Id": str(delivery.event_id),
            "DocForge-Event-Type": delivery.event_type,
            "DocForge-Signature": sign(secret, body, timestamp=int(time.time())),
        }
        try:
            with self._client() as client:
                response = client.post(hook.url, content=body, headers=headers)
        except httpx.HTTPError as error:
            return None, f"{type(error).__name__}: {error}"[:_ERROR_LIMIT]
        return response.status_code, None if response.is_success else f"HTTP {response.status_code}"
