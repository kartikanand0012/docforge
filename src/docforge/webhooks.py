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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select, text, true, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from docforge import audit
from docforge.db.models import Webhook, WebhookDelivery
from docforge.db.session import SessionFactory
from docforge.db.tenancy import current_tenant, scoped, tenant_scope
from docforge.telemetry import traced

EVENTS = ("document.processed", "document.ready_for_chat", "review.signed", "webhook.test")
MAX_WEBHOOKS = 10  # per organisation: each one is somewhere its events go
# The queue waits this long before attempt n+1: 30 s, then 60 s more each time (1.5, 2.5,
# 3.5 ... minutes). Shared with the queue, so "next retry" on the screen is the queue's.
RETRY_WAIT_SECONDS = 30
RETRY_LINEAR_SECONDS = 60
Defer = Callable[[Session, uuid.UUID, uuid.UUID], None]  # (session, delivery id, tenant id)
Outcome = Literal["delivered", "retry", "failed", "skipped"]
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_ERROR_LIMIT = 500
_DEADLINE_SECONDS = 30  # characters of an error kept, so a receiver cannot fill the table


class UnsafeDestination(ValueError):
    """The URL is not HTTPS, or its host resolves to an address that is not public."""


class TooManyWebhooks(Exception):
    """The organisation has as many webhooks as it may."""


class WebhookDisabled(Exception):
    """The webhook is disabled: enable it before sending to it."""


class DeliveryNotFailed(Exception):
    """Only a failed delivery is sent again."""


class DeliveryNotFound(LookupError):
    """No such delivery of this webhook."""


def next_wait(attempts: int) -> int:
    """Seconds the queue waits after the `attempts`-th attempt before the next."""
    return RETRY_WAIT_SECONDS + RETRY_LINEAR_SECONDS * (attempts - 1)


def host_of(url: str) -> str:
    """The host alone: a URL's path or query can hold a receiver's token."""
    return urlsplit(url).hostname or ""


# Translated IPv6 that reaches IPv4 hosts, including internal ones, through a NAT64 gateway.
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def check_destination(url: str, *, allow_http: bool = False, allow_private: bool = False) -> str:
    """The address to connect to; raises `UnsafeDestination` unless `url` may get webhooks.

    The caller must connect to the returned address, not resolve the name again: a name can
    answer differently the second time (DNS rebinding).
    """
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
    addresses = [ipaddress.ip_address(address[0]) for *_, address in found]
    for ip in addresses:
        translated = ip.version == 6 and any(ip in net for net in _NAT64)
        if not allow_private and (not ip.is_global or translated):
            raise UnsafeDestination(
                f"{parts.hostname} resolves to {ip}, which is not a public address"
            )
    if not addresses:
        raise UnsafeDestination(f"{parts.hostname} has no address")
    return str(addresses[0])


def derive_secret(key: bytes, webhook_id: uuid.UUID, version: int) -> str:
    """The webhook's signing secret: from the server key, so it is never stored."""
    digest = hmac.new(key, f"webhook:{webhook_id}:{version}".encode(), hashlib.sha256).hexdigest()
    return f"whsec_{digest}"


def sign(secret: str, body: bytes, *, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


@dataclass(frozen=True)
class DeliveryPage:
    items: list[dict[str, Any]]
    next_before: uuid.UUID | None


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
        # trust_env=False: a proxy set in the environment must not route around the checks.
        self._client = client or (
            lambda: httpx.Client(timeout=_TIMEOUT, follow_redirects=False, trust_env=False)
        )

    def _check(self, url: str) -> str:
        return check_destination(
            url, allow_http=self._allow_http, allow_private=self._allow_private
        )

    # Managing

    @scoped
    def create(
        self, tenant_id: uuid.UUID, *, url: str, events: Sequence[str], created_by: str
    ) -> tuple[uuid.UUID, str]:
        """A new webhook and its signing secret, which is shown only now."""
        unknown = sorted(set(events) - set(EVENTS))
        if not events or unknown:
            raise ValueError(f"events must be some of {', '.join(EVENTS)}")
        self._check(url)
        with self._sessions.begin() as session:
            # One maker at a time per organisation: two at once cannot both take the last place.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"webhooks:{tenant_id}"},
            )
            live = session.scalar(
                select(func.count())
                .select_from(Webhook)
                .where(Webhook.tenant_id == tenant_id, Webhook.deleted_at.is_(None))
            )
            if (live or 0) >= MAX_WEBHOOKS:
                raise TooManyWebhooks(f"an organisation has at most {MAX_WEBHOOKS} webhooks")
            webhook = Webhook(
                tenant_id=tenant_id, url=url, events=sorted(set(events)), created_by=created_by
            )
            session.add(webhook)
            session.flush()
            self._audit(
                session, webhook, created_by, "webhook.created",
                host=host_of(url), events=",".join(webhook.events),
            )  # fmt: skip
            return webhook.id, derive_secret(self._key, webhook.id, webhook.secret_version)

    @scoped
    def webhooks(self, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
        """The organisation's webhooks (deleted ones left out), each with its last delivery."""
        last = (
            select(
                WebhookDelivery.status,
                WebhookDelivery.last_status,
                WebhookDelivery.created_at,
            )
            .where(WebhookDelivery.webhook_id == Webhook.id)
            .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
            .limit(1)
            .lateral()
        )
        with self._sessions() as session:
            rows = session.execute(
                select(Webhook, last.c.status, last.c.last_status, last.c.created_at)
                .outerjoin(last, true())
                .where(Webhook.tenant_id == tenant_id, Webhook.deleted_at.is_(None))
                .order_by(Webhook.created_at)
            ).all()
            return [
                {
                    "id": hook.id,
                    "url": hook.url,
                    "events": list(hook.events),
                    "active": hook.active,
                    "created_at": hook.created_at,
                    "created_by": hook.created_by,
                    "secret_rotated_at": hook.secret_rotated_at,
                    "last_delivery": (
                        {"status": status, "last_status": code, "created_at": at}
                        if status is not None
                        else None
                    ),
                }
                for hook, status, code, at in rows
            ]

    @scoped
    def set_active(
        self, tenant_id: uuid.UUID, webhook_id: uuid.UUID, active: bool, *, actor: str
    ) -> None:
        """Disable (stop sending, keep the webhook) or enable it again. Enabling checks the
        destination again: a name can come to resolve to a local address."""
        if active:  # resolved before any lock is taken: a slow resolver holds nothing
            self._check(self._url(tenant_id, webhook_id))
        with self._sessions.begin() as session:
            hook = self._live(session, tenant_id, webhook_id)
            if hook.active != active:
                hook.active = active
                action = "webhook.enabled" if active else "webhook.disabled"
                self._audit(session, hook, actor, action, host=host_of(hook.url))

    @scoped
    def delete(self, tenant_id: uuid.UUID, webhook_id: uuid.UUID, *, actor: str) -> None:
        """Never sent to again, out of the list, its deliveries kept as a record."""
        with self._sessions.begin() as session:
            hook = self._live(session, tenant_id, webhook_id)
            hook.active, hook.deleted_at = False, datetime.now(UTC)
            self._audit(session, hook, actor, "webhook.deleted", host=host_of(hook.url))

    @scoped
    def rotate(self, tenant_id: uuid.UUID, webhook_id: uuid.UUID, *, actor: str) -> str:
        """A new signing secret, shown only now; every later attempt is signed with it,
        retries already pending included. The old one stops verifying at once."""
        with self._sessions.begin() as session:
            hook = self._live(session, tenant_id, webhook_id)
            hook.secret_version += 1
            hook.secret_rotated_at = datetime.now(UTC)
            self._audit(
                session, hook, actor, "webhook.secret_rotated", secret_version=hook.secret_version
            )
            return derive_secret(self._key, hook.id, hook.secret_version)

    @scoped
    def deliveries(
        self,
        tenant_id: uuid.UUID,
        webhook_id: uuid.UUID,
        *,
        before: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> "DeliveryPage":
        """Newest first. `before` is the last delivery already shown."""
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
            query = select(WebhookDelivery).where(
                WebhookDelivery.tenant_id == tenant_id, WebhookDelivery.webhook_id == webhook_id
            )
            if status is not None:
                query = query.where(WebhookDelivery.status == status)
            if before is not None:
                last = session.get(WebhookDelivery, before)
                if last is None or last.webhook_id != webhook_id:
                    raise DeliveryNotFound("no such delivery")
                query = query.where(
                    tuple_(WebhookDelivery.created_at, WebhookDelivery.id)
                    < tuple_(last.created_at, last.id)
                )
            rows = list(
                session.scalars(
                    query.order_by(
                        WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc()
                    ).limit(limit + 1)
                )
            )
            items = [
                {
                    "id": row.id,
                    "event_id": row.event_id,
                    "event_type": row.event_type,
                    "status": row.status,
                    "attempts": row.attempts,
                    "last_status": row.last_status,
                    "last_error": row.last_error,
                    "delivered_at": row.delivered_at,
                    "created_at": row.created_at,
                    "last_attempt_at": row.last_attempt_at,
                    "next_attempt_at": row.next_attempt_at if row.status == "pending" else None,
                }
                for row in rows[:limit]
            ]
            more = len(rows) > limit
            return DeliveryPage(items, rows[limit - 1].id if more else None)

    @scoped
    def resend(
        self, tenant_id: uuid.UUID, webhook_id: uuid.UUID, delivery_id: uuid.UUID, *, actor: str
    ) -> uuid.UUID:
        """A failed delivery, sent again with its own event id (a receiver that saw it can
        ignore it) and fresh attempts. Only a failed one: two clicks queue it once."""
        self._check(self._url(tenant_id, webhook_id))  # before any lock is taken
        with self._sessions.begin() as session:
            hook = self._live(session, tenant_id, webhook_id)
            if not hook.active:
                raise WebhookDisabled("enable the webhook first")
            previous = session.execute(
                select(WebhookDelivery.attempts, WebhookDelivery.last_status).where(
                    WebhookDelivery.id == delivery_id,
                    WebhookDelivery.tenant_id == tenant_id,
                    WebhookDelivery.webhook_id == webhook_id,
                )
            ).first()
            if previous is None:
                raise DeliveryNotFound("no such delivery")
            reset = session.execute(
                update(WebhookDelivery)
                .where(
                    WebhookDelivery.id == delivery_id,
                    WebhookDelivery.webhook_id == webhook_id,
                    WebhookDelivery.status == "failed",
                )
                .values(status="pending", attempts=0, last_error=None, next_attempt_at=None)
                .returning(WebhookDelivery.event_id, WebhookDelivery.event_type)
            ).first()
            if reset is None:
                raise DeliveryNotFailed("only a failed delivery is sent again")
            event, event_type = reset
            self._defer(session, delivery_id, tenant_id)
            self._audit(
                session, hook, actor, "webhook.delivery_resent",
                event_id=str(event), event_type=event_type,
                previous_attempts=previous.attempts, previous_status=previous.last_status,
            )  # fmt: skip
            return uuid.UUID(str(event))

    def _url(self, tenant_id: uuid.UUID, webhook_id: uuid.UUID) -> str:
        """A live webhook's URL, read without a lock (it never changes once made)."""
        with self._sessions() as session:
            url = session.scalar(
                select(Webhook.url).where(
                    Webhook.tenant_id == tenant_id,
                    Webhook.id == webhook_id,
                    Webhook.deleted_at.is_(None),
                )
            )
        if url is None:
            raise LookupError("no such webhook")
        return url

    @staticmethod
    def _live(session: Session, tenant_id: uuid.UUID, webhook_id: uuid.UUID) -> Webhook:
        """The webhook, locked for the change; a deleted one is gone."""
        hook = session.scalar(
            select(Webhook)
            .where(
                Webhook.tenant_id == tenant_id,
                Webhook.id == webhook_id,
                Webhook.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if hook is None:
            raise LookupError("no such webhook")
        return hook

    @staticmethod
    def _audit(
        session: Session, hook: Webhook, actor: str, action: str, **details: str | int | None
    ) -> None:
        """In the change's own transaction: the entry exists if and only if the change does."""
        audit.append(
            session,
            tenant_id=hook.tenant_id,
            actor=actor,
            action=action,
            target_type="webhook",
            target_id=str(hook.id),
            details=dict(details),
        )

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
        if current_tenant() != tenant_id:
            # Under row-level security an unscoped session would find no webhooks and record
            # nothing, silently.
            raise RuntimeError("emit must run inside the tenant's scope")
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
    def emit_test(self, tenant_id: uuid.UUID, webhook_id: uuid.UUID, *, actor: str) -> uuid.UUID:
        """A `webhook.test` event for this one webhook, whatever it subscribes to."""
        event_id = uuid.uuid4()
        self._check(self._url(tenant_id, webhook_id))  # before any lock is taken
        with self._sessions.begin() as session:
            hook = self._live(session, tenant_id, webhook_id)
            if not hook.active:
                raise WebhookDisabled("enable the webhook first")
            delivery_id = session.execute(
                insert(WebhookDelivery)
                .values(
                    tenant_id=tenant_id,
                    webhook_id=hook.id,
                    event_id=event_id,
                    event_type="webhook.test",
                    payload={
                        "id": str(event_id),
                        "type": "webhook.test",
                        "created_at": datetime.now(UTC).isoformat(),
                        "data": {"message": "test from DocForge"},
                    },
                )
                .returning(WebhookDelivery.id)
            ).scalar_one()
            self._defer(session, delivery_id, tenant_id)
            self._audit(session, hook, actor, "webhook.test_sent", event_id=str(event_id))
        return event_id

    def deliver(self, delivery_id: uuid.UUID, tenant_id: uuid.UUID) -> Outcome:
        """One attempt. `retry` means the queue should call again later.

        The attempt is claimed in one short transaction, the request is sent with no
        transaction open, and the result recorded in another: a slow receiver holds no lock
        and no connection. If the process dies between sending and recording, the event is
        sent again later with the same id, which the receiver can recognise.
        """
        with traced("webhook.deliver") as span:
            span.set_attribute("docforge.delivery_id", str(delivery_id))
            outcome = self._deliver(delivery_id, tenant_id)
            span.set_attribute("docforge.outcome", outcome)
            return outcome

    def _deliver(self, delivery_id: uuid.UUID, tenant_id: uuid.UUID) -> Outcome:
        with tenant_scope(tenant_id):
            with self._sessions.begin() as session:
                row = session.execute(
                    select(WebhookDelivery, Webhook)
                    .join(Webhook, Webhook.id == WebhookDelivery.webhook_id)
                    .where(WebhookDelivery.id == delivery_id)
                    .with_for_update(of=WebhookDelivery, skip_locked=True)
                ).first()
                if row is None:
                    return "skipped"
                delivery, hook = row
                if delivery.status != "pending":
                    return "skipped"
                if not hook.active:
                    gone = "removed" if hook.deleted_at is not None else "disabled"
                    delivery.status, delivery.last_error = "failed", f"the webhook was {gone}"
                    delivery.next_attempt_at = None
                    return "failed"
                delivery.attempts += 1
                delivery.last_attempt_at = datetime.now(UTC)
                attempt = delivery.attempts
                request = (hook.url, hook.id, hook.secret_version, delivery.event_id)
                event_type, payload = delivery.event_type, dict(delivery.payload)
            status, error = self._send(*request, event_type, payload)
            with self._sessions.begin() as session:
                delivery = session.get_one(WebhookDelivery, delivery_id)
                delivery.last_status, delivery.last_error = status, error
                if status is not None and 200 <= status < 300:
                    delivery.status, delivery.delivered_at = "delivered", datetime.now(UTC)
                    delivery.next_attempt_at = None
                    return "delivered"
                if attempt >= self.max_attempts:
                    delivery.status, delivery.next_attempt_at = "failed", None
                    return "failed"
                # When the queue will try again: shown on the screen as "about".
                delivery.next_attempt_at = datetime.now(UTC) + timedelta(seconds=next_wait(attempt))
                return "retry"

    def _send(
        self,
        url: str,
        webhook_id: uuid.UUID,
        secret_version: int,
        event_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> tuple[int | None, str | None]:
        """POST once. Never raises: any failure comes back as an error to record."""
        try:
            address = self._check(url)
        except UnsafeDestination:
            # Not what the name resolved to: an error a person reads must not map the network.
            return None, "the destination is not allowed (it must be https and public)"
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        secret = derive_secret(self._key, webhook_id, secret_version)
        parts = urlsplit(url)
        host = parts.hostname or ""
        authority = f"{host}:{parts.port}" if parts.port else host
        # Connect to the address that was checked; keep the name for Host and for TLS.
        pinned = f"[{address}]" if ":" in address else address
        target = parts._replace(netloc=f"{pinned}:{parts.port}" if parts.port else pinned)
        headers = {
            "Host": authority,
            "Content-Type": "application/json",
            "User-Agent": "DocForge-Webhooks/1",
            "DocForge-Event-Id": str(event_id),
            "DocForge-Event-Type": event_type,
            "DocForge-Signature": sign(secret, body, timestamp=int(time.time())),
        }
        deadline = time.monotonic() + _DEADLINE_SECONDS
        try:
            # Streamed and not read: the answer's status is all that is needed, and a
            # receiver cannot make us hold a large or endless body.
            with (
                self._client() as client,
                client.stream(
                    "POST",
                    target.geturl(),
                    content=body,
                    headers=headers,
                    extensions={"sni_hostname": host},
                ) as response,
            ):
                status = response.status_code
        except Exception as error:  # any failure is an outcome to record, not a crash
            return None, f"{type(error).__name__}: {error}"[:_ERROR_LIMIT]
        if time.monotonic() > deadline:
            return None, "the receiver took too long to answer"
        return status, None if 200 <= status < 300 else f"HTTP {status}"
