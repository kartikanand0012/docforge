"""Invoices by email (V2 P5): the mail provider's way in, Settings > Intake, and the Intake
list.

The two inbound routes are called by the mail provider, not a person: they need HTTP Basic
with `INTAKE_WEBHOOK_PASSWORD` (Postmark sends it from the credentials in its webhook URL)
and answer 200 for any mail they could read, so the provider does not send it again; what
became of it is in the workspace's Intake list. They are refused (503) when intake is not set
up. `/v1/intake/inbound` takes the provider-agnostic form; `/v1/intake/inbound/postmark` is
the one provider adapter.
"""

import base64
import binascii
import secrets
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from docforge.api.auth import NOT_ALLOWED, current_principal, require
from docforge.auth import MEMBER, Principal
from docforge.intake.model import (
    MAX_ATTACHMENTS,
    InboundAttachment,
    InboundEmail,
    PayloadInvalid,
    address_of,
    decode,
)
from docforge.intake.postmark import from_postmark
from docforge.intake.service import (
    AddressView,
    IntakeInvalid,
    IntakeNotFound,
    IntakeService,
    MessageView,
    NoAddress,
)

Reader = Annotated[Principal, Depends(require("documents:read"))]
Reviewer = Annotated[Principal, Depends(require("review"))]


async def _may_change(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    """As for the workspace's other settings: its admins, or a personal workspace's person."""
    if not (principal.can("admin") or principal.role == MEMBER):
        raise HTTPException(403, NOT_ALLOWED)
    return principal


Admin = Annotated[Principal, Depends(_may_change)]
INBOUND_PREFIX = "/v1/intake/inbound"
MAX_INBOUND_BYTES = 50 * 1024 * 1024  # a mail's attachments, base64 (Postmark allows 35 MB)


class AttachmentIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filename: str = Field(max_length=500)
    content_type: str | None = Field(default=None, max_length=200)
    content: str  # base64
    inline: bool = False


class InboundIn(BaseModel):
    """A parsed email, as any provider's adapter can give it."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    from_: str = Field(alias="from", max_length=500)
    to: list[str] = Field(max_length=100)
    subject: str = Field(default="", max_length=2000)
    message_id: str | None = Field(default=None, max_length=1000)
    attachments: list[AttachmentIn] = Field(default_factory=list)


class InboundOut(BaseModel):
    delivered: int  # workspaces the mail reached (0 for an address that is no one's)


class IntakeItemOut(BaseModel):
    id: uuid.UUID
    position: int
    filename: str
    size_bytes: int
    outcome: str  # received, already_received, ignored, refused, held, discarded
    reason: str | None
    document_id: uuid.UUID | None
    document_status: str | None  # failed: its reading failed (retry it)


class IntakeMessageOut(BaseModel):
    id: uuid.UUID
    sender: str
    subject: str
    status: str  # received, held, rejected, accepted, discarded
    reason: str | None
    received_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    items: list[IntakeItemOut]


class AddressOut(BaseModel):
    address: str  # whole only in the answer to making it; hidden after
    whole: bool
    created_by: str
    created_at: datetime


class SenderOut(BaseModel):
    id: uuid.UUID
    sender: str  # name@domain.in, or @domain.in for the whole domain
    created_by: str
    created_at: datetime


class IntakeSettingsOut(BaseModel):
    enabled: bool  # an intake domain is set up for this deployment
    domain: str | None
    address: AddressOut | None
    senders: list[SenderOut]
    trust_accepted: bool  # list a sender once a held mail of theirs is accepted
    per_sender_hourly: int


class IntakeSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trust_accepted: bool | None = None
    per_sender_hourly: int | None = Field(default=None, ge=1, le=10_000)


class SenderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender: str = Field(max_length=254)


def _message(view: MessageView) -> IntakeMessageOut:
    return IntakeMessageOut(
        id=view.id, sender=view.sender, subject=view.subject, status=view.status,
        reason=view.reason, received_at=view.received_at, decided_by=view.decided_by,
        decided_at=view.decided_at,
        items=[IntakeItemOut(id=i.id, position=i.position, filename=i.filename,
                       size_bytes=i.size_bytes, outcome=i.outcome, reason=i.reason,
                       document_id=i.document_id, document_status=i.document_status)
               for i in view.items],
    )  # fmt: skip


def _address(view: AddressView, *, whole: bool) -> AddressOut:
    return AddressOut(
        address=view.shown, whole=whole, created_by=view.created_by, created_at=view.created_at
    )


def intake_router(intake: IntakeService, *, webhook_password: str | None) -> APIRouter:
    router = APIRouter()

    def provider(request: Request) -> None:
        """HTTP Basic with the webhook password, compared in constant time."""
        if not webhook_password or not intake.domain:
            raise HTTPException(503, "Email intake is not set up on this server.")
        scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
        try:
            given = base64.b64decode(encoded, validate=True).decode().partition(":")[2]
        except (binascii.Error, UnicodeDecodeError):
            given = ""
        if scheme.lower() != "basic" or not secrets.compare_digest(
            given.encode(), webhook_password.encode()
        ):
            raise HTTPException(
                401, "Not authorised.", headers={"WWW-Authenticate": 'Basic realm="intake"'}
            )

    provider_only = Depends(provider)

    def deliver(email: InboundEmail) -> InboundOut:
        return InboundOut(delivered=len(intake.receive(email)))

    @router.post(INBOUND_PREFIX, response_model=InboundOut, dependencies=[provider_only])
    def inbound(body: InboundIn) -> InboundOut:
        """A parsed email in the provider-agnostic form."""
        try:
            attachments = tuple(
                InboundAttachment(a.filename, a.content_type, decode(a.content), a.inline)
                for a in body.attachments[:MAX_ATTACHMENTS]
            )
        except PayloadInvalid as error:
            raise HTTPException(422, str(error)) from error
        email = InboundEmail(
            sender=address_of(body.from_), recipients=tuple(address_of(t) for t in body.to),
            subject=body.subject, message_id=body.message_id, attachments=attachments,
        )  # fmt: skip
        return deliver(email)

    @router.post(
        f"{INBOUND_PREFIX}/postmark", response_model=InboundOut, dependencies=[provider_only]
    )
    def inbound_postmark(payload: dict[str, Any]) -> InboundOut:
        """Postmark's inbound webhook."""
        try:
            return deliver(from_postmark(payload))
        except PayloadInvalid as error:
            raise HTTPException(422, str(error)) from error

    # Settings > Intake (admins)

    def _settings(tenant_id: uuid.UUID, made: AddressView | None = None) -> IntakeSettingsOut:
        address = made or intake.address(tenant_id)
        settings = intake.settings(tenant_id)
        senders = [
            SenderOut(id=s.id, sender=s.pattern, created_by=s.created_by,
                      created_at=s.created_at)
            for s in intake.senders(tenant_id)
        ]  # fmt: skip
        return IntakeSettingsOut(
            enabled=intake.domain is not None, domain=intake.domain,
            address=_address(address, whole=made is not None) if address else None,
            senders=senders, trust_accepted=settings.trust_accepted,
            per_sender_hourly=settings.per_sender_hourly,
        )  # fmt: skip

    @router.get("/v1/settings/intake", response_model=IntakeSettingsOut)
    def read_settings(principal: Admin) -> IntakeSettingsOut:
        """The workspace's address (its token hidden), its senders and its limits."""
        return _settings(principal.tenant_id)

    @router.post("/v1/settings/intake/address", response_model=IntakeSettingsOut,
                 status_code=201)  # fmt: skip
    def make_address(principal: Admin) -> IntakeSettingsOut:
        """A new address, shown whole in this answer only. Any address before it stops now:
        mail to it makes nothing."""
        made = intake.make_address(
            principal.tenant_id, actor=principal.actor, person=principal.name
        )
        return _settings(principal.tenant_id, made)

    @router.patch("/v1/settings/intake", response_model=IntakeSettingsOut)
    def change_settings(body: IntakeSettingsIn, principal: Admin) -> IntakeSettingsOut:
        try:
            intake.update_settings(
                principal.tenant_id, body.model_dump(exclude_none=True),
                actor=principal.actor, person=principal.name,
            )  # fmt: skip
        except IntakeInvalid as error:
            raise HTTPException(422, str(error)) from error
        return _settings(principal.tenant_id)

    @router.post("/v1/settings/intake/senders", response_model=SenderOut, status_code=201)
    def add_sender(body: SenderIn, principal: Admin) -> SenderOut:
        """A whole address, or @ and a domain for everyone at it."""
        try:
            s = intake.add_sender(
                principal.tenant_id, body.sender, actor=principal.actor, person=principal.name
            )
        except IntakeInvalid as error:
            raise HTTPException(422, str(error)) from error
        return SenderOut(id=s.id, sender=s.pattern, created_by=s.created_by,
                         created_at=s.created_at)  # fmt: skip

    @router.delete("/v1/settings/intake/senders/{sender_id}", status_code=204)
    def remove_sender(sender_id: uuid.UUID, principal: Admin) -> None:
        try:
            intake.remove_sender(principal.tenant_id, sender_id, actor=principal.actor)
        except IntakeNotFound as error:
            raise HTTPException(404, "No such sender on the list.") from error

    @router.post("/v1/settings/intake/test", response_model=IntakeMessageOut)
    def test(principal: Admin) -> IntakeMessageOut:
        """Put a made-up invoice through intake as mail from DocForge; watch it arrive in the
        Intake list and the documents."""
        try:
            return _message(intake.test(principal.tenant_id))
        except NoAddress as error:
            raise HTTPException(409, "Make the workspace's address first.") from error

    # The Intake list

    @router.get("/v1/intake/messages", response_model=list[IntakeMessageOut])
    def list_messages(
        principal: Reader,
        view: Literal["all", "held", "failed", "ignored", "rejected"] = "all",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[IntakeMessageOut]:
        """Mail received, newest first, with what became of each attachment."""
        return [_message(m) for m in intake.messages(principal.tenant_id, view=view, limit=limit)]

    @router.post("/v1/intake/messages/{message_id}/accept", response_model=IntakeMessageOut)
    def accept(message_id: uuid.UUID, principal: Reviewer) -> IntakeMessageOut:
        """Read the mail's held attachments now."""
        try:
            view = intake.accept(
                principal.tenant_id, message_id, actor=principal.actor, person=principal.name
            )
        except IntakeNotFound as error:
            raise HTTPException(404, "No such mail.") from error
        except IntakeInvalid as error:
            raise HTTPException(409, str(error)) from error
        return _message(view)

    @router.post("/v1/intake/messages/{message_id}/discard", response_model=IntakeMessageOut)
    def discard(message_id: uuid.UUID, principal: Reviewer) -> IntakeMessageOut:
        """Delete the mail's held copies."""
        try:
            view = intake.discard(
                principal.tenant_id, message_id, actor=principal.actor, person=principal.name
            )
        except IntakeNotFound as error:
            raise HTTPException(404, "No such mail.") from error
        except IntakeInvalid as error:
            raise HTTPException(409, str(error)) from error
        return _message(view)

    @router.post("/v1/intake/items/{item_id}/retry", status_code=202)
    def retry(item_id: uuid.UUID, principal: Reviewer) -> None:
        """Read again an attachment whose reading failed."""
        try:
            intake.retry(principal.tenant_id, item_id, actor=principal.actor)
        except IntakeNotFound as error:
            raise HTTPException(404, "No such attachment, or it made no document.") from error
        except IntakeInvalid as error:
            raise HTTPException(409, str(error)) from error

    return router
