"""Invoices by email (V2 P5): each workspace's forwarding address, its allowlist, and what
becomes of every mail and attachment sent to it.

A mail is matched to its workspace by the token in its address alone (`<slug>-<token>@<domain>`;
only the token's SHA-256 is kept). Then each attachment is taken on its own, so one that fails
cannot stop the rest, nor any later mail:

1. ignored, with the reason recorded: `winmail.dat`, text notes, and small images (logos,
   signatures) - none of them a bill;
2. refused by the upload's own checks (`docforge.upload_checks`), with the upload's words;
3. held for a reviewer when the sender is not on the workspace's list, past the sender's hourly
   limit, or when the processing queue is nearly full (uploads keep the last places);
4. otherwise stored and queued through `DocumentService.ingest`, the upload's own entry
   point (`source="email"`), so the same bytes from any channel are one document.

Held attachments are kept under `intake/{workspace}/` until a reviewer accepts them (they are
read then) or discards them (they are deleted); never by themselves. A listed sender gets a
plain reply per attachment - never a value read from a bill.
"""

import base64
import hashlib
import io
import logging
import re
import secrets
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from docforge import audit
from docforge.accounts import MemberCaps
from docforge.api.uploads import safe_filename
from docforge.db.models import (
    Document,
    IntakeAddress,
    IntakeItem,
    IntakeMessage,
    IntakeSender,
    IntakeSettings,
    Tenant,
)
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.documents import (
    DailyReadingsUsed,
    DocumentService,
    DocumentTypeConflict,
    FreeReadingsUsed,
    QueueFull,
    ReprocessInProgress,
    WorkspaceFull,
)
from docforge.formats import sniff
from docforge.intake.mailer import Mailer, NoMailer, Reply
from docforge.intake.model import InboundAttachment, InboundEmail, address_of
from docforge.storage import ObjectStore
from docforge.upload_checks import FileRefused, check_file

logger = logging.getLogger(__name__)

ACTOR = "intake:email"
TOKEN_LENGTH = 26  # 128 random bits in lower-case base32
SMALL_IMAGE_BYTES = 30_000  # a logo or a signature; a photo of a bill is far larger
QUEUE_RESERVE = 100  # places in the processing queue left to uploads
MAX_SENDERS = 500
Outcome = Literal["received", "already_received", "ignored", "refused", "held", "discarded"]
View = Literal["all", "held", "failed", "ignored", "rejected"]

WINMAIL = "winmail.dat holds Outlook's formatting, not a bill"
TEXT_NOTE = "a text note, not a bill"
SMALL_IMAGE = "a small image (a logo or a signature), not a bill"
NOT_LISTED = "the sender is not on this workspace's list"
QUEUE_BUSY = "queue busy: the processing queue is nearly full"
REVOKED = "address no longer in use"
STORE_FAILED = "it could not be stored just now: forward it again later"
_TEXT_TYPES = ("text/", "message/")
_TEXT_ENDINGS = (".txt", ".htm", ".html", ".ics", ".vcf", ".eml", ".csv", ".rtf")
_AUTOMATED = re.compile(r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounce)", re.I)
_TOKEN = re.compile(rf"[a-z2-7]{{{TOKEN_LENGTH}}}")
_SLUG = re.compile(r"[^a-z0-9]+")


class IntakeInvalid(ValueError):
    """Something intake cannot take, said in words."""


class IntakeNotFound(LookupError):
    """No such mail, attachment or sender in this workspace."""


class NoAddress(LookupError):
    """The workspace has no active address."""


def new_token() -> str:
    return base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=").lower()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.lower().encode()).hexdigest()


def slug_of(name: str) -> str:
    return _SLUG.sub("-", name.casefold()).strip("-")[:20].strip("-") or "workspace"


def token_of(address: str, domain: str | None) -> str | None:
    """The token in `<slug>-<token>@<domain>`, or None for any other address."""
    local, _, host = address_of(address).rpartition("@")
    if not local or (domain and host != domain.lower()):
        return None
    token = local.rsplit("-", 1)[-1]
    return token if _TOKEN.fullmatch(token) else None


def listed(sender: str, patterns: Sequence[str]) -> bool:
    """On the list: the whole address, or `@` its domain."""
    domain = sender.rpartition("@")[2]
    return sender in patterns or f"@{domain}" in patterns


def pattern_of(value: str) -> str:
    """An allowlist entry, checked: `name@domain.in` or `@domain.in`. Raises IntakeInvalid."""
    entry = value.strip().lower()
    local, at, domain = entry.rpartition("@")
    if not at or "." not in domain or " " in entry or not 3 <= len(entry) <= 254:
        raise IntakeInvalid(
            f"'{value.strip()}' is neither an email address nor @ and a domain "
            "(such as @supplier.in)."
        )
    return entry if local else f"@{domain}"


def ignored_because(attachment: InboundAttachment) -> str | None:
    name = attachment.filename.casefold()
    kind = (attachment.content_type or "").casefold()
    if name == "winmail.dat" or kind == "application/ms-tnef":
        return WINMAIL
    if kind.startswith(_TEXT_TYPES) or name.endswith(_TEXT_ENDINGS):
        return TEXT_NOTE
    small = len(attachment.data) < SMALL_IMAGE_BYTES
    if small and sniff(attachment.data) in ("png", "jpeg", "tiff"):
        return SMALL_IMAGE
    return None


@dataclass(frozen=True)
class ItemView:
    id: uuid.UUID
    position: int
    filename: str
    size_bytes: int
    outcome: str  # received, already_received, ignored, refused, held, discarded
    reason: str | None
    document_id: uuid.UUID | None
    document_status: str | None  # the document's: received, processing, ready, failed ...


@dataclass(frozen=True)
class MessageView:
    id: uuid.UUID
    sender: str
    subject: str
    status: str  # received, held, rejected, accepted, discarded
    reason: str | None
    received_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    items: tuple[ItemView, ...] = field(default=())


@dataclass(frozen=True)
class AddressView:
    slug: str
    shown: str  # whole when just made; otherwise with its token hidden
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class SenderView:
    id: uuid.UUID
    pattern: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class SettingsView:
    trust_accepted: bool
    per_sender_hourly: int


_REPLY_WORDS = {
    "received": "received, being read",
    "already_received": "already received earlier, so nothing new was made",
    "ignored": "not read: {reason}",
    "refused": "not accepted: {reason}",
    "held": "held for review: {reason}",
}


def confirmation(subject: str, workspace: str, items: Sequence[ItemView], link: str | None) -> str:
    """The reply's body: per attachment what became of it, in plain words. Never a value read
    from a bill: email is no secure channel, and nothing read is sent out by it."""
    lines = [f'DocForge received your email "{subject or "(no subject)"}" for {workspace}.', ""]
    if not items:
        lines.append("It had no attachments, so there was nothing to read.")
    for item in items:
        words = _REPLY_WORDS.get(item.outcome, item.outcome).format(reason=item.reason or "")
        lines.append(f"- {item.filename}: {words.rstrip('.')}.")
    lines += [
        "",
        "A PDF holding several bills is divided into one document per bill; where its pages "
        "are unclear, a person in your workspace groups them first.",
    ]
    if link:
        lines.append(f"See them in DocForge: {link}")
    lines += ["", "This is an automatic reply."]
    return "\n".join(lines)


def _clean(subject: str) -> str:
    return " ".join("".join(c for c in subject if c.isprintable()).split())[:300]


def sample_invoice() -> bytes:
    """A made-up one-page invoice for the intake test, new bytes each time (its time printed),
    so every test makes a document of its own."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = [
        "TAX INVOICE (SAMPLE - made up by DocForge's intake test)",
        "Seller: Sample Pharma Distributors   GSTIN: 24AABFN7754K1Z0",
        f"Invoice No: TEST/{secrets.token_hex(3).upper()}   Date: {now[:10]}",
        "Paracetamol 500mg Tab 10x10   Batch SB101   Exp 12/28   Qty 10   Rate 18.00",
        "Taxable 180.00   CGST 6% 10.80   SGST 6% 10.80   Total 201.60",
        f"Generated {now}",
    ]
    for at, row in enumerate(rows):
        page.drawString(50, 790 - at * 22, row)
    page.showPage()
    page.save()
    return buffer.getvalue()


class IntakeService:
    def __init__(
        self,
        sessions: SessionFactory,
        documents: DocumentService,
        store: ObjectStore,
        mailer: Mailer | None = None,
        *,
        domain: str | None,
        web_url: str | None = None,
        max_upload_bytes: int = 10 * 1024 * 1024,
        max_pages: int = 20,
        member_caps: MemberCaps | None = None,
    ) -> None:
        self._sessions = sessions
        self._documents = documents
        self._store = store
        self._mailer = mailer or NoMailer()
        self.domain = domain.lower() if domain else None
        self._web_url = web_url.rstrip("/") if web_url else None
        self._max_upload_bytes = max_upload_bytes
        self._max_pages = max_pages
        self._caps = member_caps or MemberCaps()

    # --- receiving ---------------------------------------------------------------------

    def receive(self, email: InboundEmail) -> list[MessageView]:
        """Deliver a mail to each workspace whose address it was sent to. An address that is
        no one's is ignored, and nothing is said to whoever sent it."""
        sender = address_of(email.sender)
        if not sender or (self.domain and sender.endswith(f"@{self.domain}")):
            return []  # nothing from our own domain: a reply must never come back in
        delivered: list[MessageView] = []
        seen: set[uuid.UUID] = set()
        for recipient in email.recipients:
            token = token_of(recipient, self.domain)
            if token is None:
                continue
            with self._sessions() as session:
                found = session.execute(
                    text("SELECT tenant_id, address_id, active FROM docforge_intake_address(:h)"),
                    {"h": token_hash(token)},
                ).first()
            if found is None or found.tenant_id in seen:
                continue
            seen.add(found.tenant_id)
            delivered.append(self._deliver(found.tenant_id, found.address_id, found.active, email))
        return delivered

    @scoped
    def _deliver(
        self,
        tenant_id: uuid.UUID,
        address_id: uuid.UUID,
        active: bool,
        email: InboundEmail,
        *,
        trusted: bool = False,
    ) -> MessageView:
        sender = address_of(email.sender)
        subject = _clean(email.subject)
        with self._sessions() as session:
            patterns = list(
                session.scalars(
                    select(IntakeSender.pattern).where(IntakeSender.tenant_id == tenant_id)
                )
            )
            settings = self._settings(session, tenant_id)
            tenant = session.get(Tenant, tenant_id)
            workspace = (tenant.display_name or tenant.name) if tenant else "your workspace"
            free = tenant is not None and tenant.kind == "personal"
        allowed = trusted or listed(sender, patterns)
        status, reason = "received", None
        if not active:
            status, reason = "rejected", REVOKED
        elif not allowed:
            status, reason = "held", NOT_LISTED
        with self._sessions.begin() as session:
            message = IntakeMessage(
                tenant_id=tenant_id, address_id=address_id, sender=sender[:254],
                subject=subject, provider_message_id=(email.message_id or "")[:300] or None,
                status=status, reason=reason,
            )  # fmt: skip
            session.add(message)
            session.flush()
            message_id = message.id
        items: list[ItemView] = []
        if active:
            for position, attachment in enumerate(email.attachments):
                items.append(
                    self._item(tenant_id, message_id, position, attachment, sender,
                               allowed=allowed, hourly=settings.per_sender_hourly, free=free)
                )  # fmt: skip
        counts = {o: sum(1 for i in items if i.outcome == o) for o in
                  ("received", "already_received", "ignored", "refused", "held")}  # fmt: skip
        with self._sessions.begin() as session:
            audit.append(
                session, tenant_id=tenant_id, actor=ACTOR,
                action="intake.held" if status == "held" else "intake.received",
                target_type="intake_message", target_id=str(message_id),
                details={"sender": sender, "status": status, "reason": reason, **counts},
            )  # fmt: skip
        view = self.message(tenant_id, message_id)
        if active and allowed and not trusted:
            self._reply(workspace, email, view)
        return view

    def _item(
        self,
        tenant_id: uuid.UUID,
        message_id: uuid.UUID,
        position: int,
        attachment: InboundAttachment,
        sender: str,
        *,
        allowed: bool,
        hourly: int,
        free: bool,
    ) -> ItemView:
        """One attachment, on its own: whatever happens to it, the others go on."""
        filename = safe_filename(attachment.filename) or f"attachment-{position + 1}"
        data = attachment.data
        item_id = uuid.uuid4()
        outcome: Outcome = "received"
        reason: str | None = None
        document_id: uuid.UUID | None = None
        held_key: str | None = None
        try:
            ignored = ignored_because(attachment)
            if ignored is not None:
                outcome, reason = "ignored", ignored
            else:
                # An invoice PDF may hold several invoices, divided as an upload's is (P6).
                pages = self._documents.pages_allowed("invoice", self._max_pages)
                check_file(data, max_upload_bytes=self._max_upload_bytes, max_pages=pages)
                hold = self._hold_reason(tenant_id, sender, allowed=allowed, hourly=hourly)
                if hold is None:
                    outcome, reason, document_id = self._ingest(tenant_id, filename, data, free)
                if hold is not None or outcome == "held":
                    held_key = f"intake/{tenant_id}/{item_id}"
                    self._store.put(held_key, data, "application/octet-stream")
                    outcome, reason = "held", reason or hold
        except FileRefused as error:
            outcome, reason = "refused", error.message
        except Exception:  # never lost: recorded, and the rest of the mail goes on
            logger.exception("intake: an attachment could not be taken")
            outcome, reason, held_key, document_id = "refused", STORE_FAILED, None, None
        with self._sessions.begin() as session:
            session.add(
                IntakeItem(
                    id=item_id, tenant_id=tenant_id, message_id=message_id, position=position,
                    filename=filename, size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(), outcome=outcome, reason=reason,
                    document_id=document_id, held_key=held_key,
                )
            )  # fmt: skip
        return ItemView(item_id, position, filename, len(data), outcome, reason, document_id, None)

    def _hold_reason(
        self, tenant_id: uuid.UUID, sender: str, *, allowed: bool, hourly: int
    ) -> str | None:
        if not allowed:
            return NOT_LISTED
        since = datetime.now(UTC) - timedelta(hours=1)
        with self._sessions() as session:
            made = session.scalar(
                select(func.count(IntakeItem.id))
                .join(IntakeMessage, IntakeMessage.id == IntakeItem.message_id)
                .where(
                    IntakeItem.tenant_id == tenant_id,
                    IntakeMessage.sender == sender,
                    IntakeItem.outcome == "received",
                    IntakeItem.created_at >= since,
                )
            )
        if (made or 0) >= hourly:
            return f"rate limit: {hourly} documents an hour from one sender"
        if self._documents.pending(tenant_id) >= self._documents.max_pending - QUEUE_RESERVE:
            return QUEUE_BUSY
        return None

    def _ingest(
        self, tenant_id: uuid.UUID, filename: str, data: bytes, free: bool
    ) -> tuple[Outcome, str | None, uuid.UUID | None]:
        """Through the upload's own entry point. A full queue holds it; a cap refuses it."""
        try:
            result = self._documents.ingest(
                tenant_id=tenant_id, doc_type="invoice", filename=filename, data=data,
                actor=ACTOR, source="email",
                max_documents=self._caps.max_documents if free else None,
                readings_per_day=self._caps.uploads_per_day if free else None,
                free_readings_per_day=self._caps.readings_per_day_total if free else None,
            )  # fmt: skip
        except QueueFull:
            return "held", QUEUE_BUSY, None
        except WorkspaceFull:
            return "refused", "this free workspace is full: delete a document first", None
        except (DailyReadingsUsed, FreeReadingsUsed):
            return "refused", "today's readings are used up: send it again tomorrow", None
        except DocumentTypeConflict as error:
            return "refused", str(error), None
        return ("received" if result.created else "already_received"), None, result.document.id

    def _reply(self, workspace: str, email: InboundEmail, view: MessageView) -> None:
        if _AUTOMATED.match(view.sender.partition("@")[0]):
            return  # never answer a machine
        link = f"{self._web_url}/documents?source=email" if self._web_url else None
        reply = Reply(
            to=view.sender,
            subject=f"Received: {view.subject}"[:200] if view.subject else "Received",
            body=confirmation(view.subject, workspace, view.items, link),
            in_reply_to=email.message_id,
        )
        try:
            self._mailer.send(reply)
        except Exception:  # a reply that cannot be sent never undoes the intake
            logger.warning("intake: the reply could not be sent", exc_info=True)

    # --- the Intake list ----------------------------------------------------------------

    @scoped
    def message(self, tenant_id: uuid.UUID, message_id: uuid.UUID) -> MessageView:
        with self._sessions() as session:
            row = self._message_row(session, tenant_id, message_id)
            return self._views(session, tenant_id, [row])[0]

    @scoped
    def messages(
        self, tenant_id: uuid.UUID, *, view: View = "all", limit: int = 50
    ) -> list[MessageView]:
        """Newest first. `held`: mail with an attachment held; `failed`: one whose reading
        failed; `ignored`: one ignored or refused; `rejected`: mail to a revoked address."""
        limit = min(max(limit, 1), 200)
        with self._sessions() as session:
            rows = list(
                session.scalars(
                    select(IntakeMessage)
                    .where(IntakeMessage.tenant_id == tenant_id)
                    .order_by(IntakeMessage.received_at.desc(), IntakeMessage.id.desc())
                    .limit(limit if view == "all" else limit * 10)
                )
            )
            found = self._views(session, tenant_id, rows)

        def wanted(m: MessageView) -> bool:
            if view == "held":
                return any(i.outcome == "held" for i in m.items)
            if view == "failed":
                return any(i.document_status == "failed" for i in m.items)
            if view == "ignored":
                return any(i.outcome in ("ignored", "refused") for i in m.items)
            if view == "rejected":
                return m.status == "rejected"
            return True

        return [m for m in found if wanted(m)][:limit]

    @staticmethod
    def _views(
        session: Session, tenant_id: uuid.UUID, rows: Sequence[IntakeMessage]
    ) -> list[MessageView]:
        if not rows:
            return []
        items = session.execute(
            select(IntakeItem, Document.status)
            .outerjoin(
                Document,
                (Document.id == IntakeItem.document_id) & (Document.tenant_id == tenant_id),
            )
            .where(
                IntakeItem.tenant_id == tenant_id,
                IntakeItem.message_id.in_([r.id for r in rows]),
            )
            .order_by(IntakeItem.position)
        ).all()
        by_message: dict[uuid.UUID, list[ItemView]] = {}
        for item, status in items:
            by_message.setdefault(item.message_id, []).append(
                ItemView(item.id, item.position, item.filename, item.size_bytes, item.outcome,
                         item.reason, item.document_id, status)
            )  # fmt: skip
        return [
            MessageView(
                id=r.id,
                sender=r.sender,
                subject=r.subject,
                status=r.status,
                reason=r.reason,
                received_at=r.received_at,
                decided_by=r.decided_by,
                decided_at=r.decided_at,
                items=tuple(by_message.get(r.id, ())),
            )
            for r in rows
        ]

    @staticmethod
    def _message_row(
        session: Session, tenant_id: uuid.UUID, message_id: uuid.UUID
    ) -> IntakeMessage:
        row = session.scalars(
            select(IntakeMessage).where(
                IntakeMessage.tenant_id == tenant_id, IntakeMessage.id == message_id
            )
        ).one_or_none()
        if row is None:
            raise IntakeNotFound(message_id)
        return row

    def _held(self, session: Session, tenant_id: uuid.UUID, message_id: uuid.UUID) -> list[Any]:
        return list(
            session.scalars(
                select(IntakeItem).where(
                    IntakeItem.tenant_id == tenant_id,
                    IntakeItem.message_id == message_id,
                    IntakeItem.outcome == "held",
                )
            )
        )

    @scoped
    def accept(
        self, tenant_id: uuid.UUID, message_id: uuid.UUID, *, actor: str, person: str
    ) -> MessageView:
        """Read the mail's held attachments now, as if its sender were listed. With the
        workspace's "trust a sender once accepted" on, the sender is listed from now on.
        Raises `IntakeNotFound` or `IntakeInvalid`."""
        with self._sessions() as session:
            message = self._message_row(session, tenant_id, message_id)
            held = [(i.id, i.filename, i.held_key) for i in self._held(session, tenant_id,
                                                                       message_id)]  # fmt: skip
            settings = self._settings(session, tenant_id)
            sender, reason = message.sender, message.reason
            tenant = session.get(Tenant, tenant_id)
            free = tenant is not None and tenant.kind == "personal"
        if not held:
            raise IntakeInvalid("This mail has nothing held to accept.")
        documents = 0
        for item_id, filename, key in held:
            outcome, why, document_id = self._ingest(
                tenant_id, filename, self._store.get(key or ""), free
            )
            if outcome == "held":
                continue  # the queue is still full: it stays held
            with self._sessions.begin() as session:
                row = session.get_one(IntakeItem, item_id)
                row.outcome, row.reason, row.document_id = outcome, why, document_id
                row.held_key, row.updated_at = None, datetime.now(UTC)
            if key:
                self._store.delete(key)
            documents += outcome == "received"
        with self._sessions.begin() as session:
            message = self._message_row(session, tenant_id, message_id)
            message.status = "accepted"
            message.decided_by, message.decided_at = person[:200], datetime.now(UTC)
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="intake.accepted",
                target_type="intake_message", target_id=str(message_id),
                details={"sender": sender, "documents": documents},
            )  # fmt: skip
        if settings.trust_accepted and reason == NOT_LISTED:
            with suppress(IntakeInvalid):  # listed already
                self.add_sender(tenant_id, sender, actor=actor, person=person)
        return self.message(tenant_id, message_id)

    @scoped
    def discard(
        self, tenant_id: uuid.UUID, message_id: uuid.UUID, *, actor: str, person: str
    ) -> MessageView:
        """Delete the mail's held copies; the record of the mail stays. Raises
        `IntakeNotFound` or `IntakeInvalid`."""
        with self._sessions.begin() as session:
            message = self._message_row(session, tenant_id, message_id)
            held = self._held(session, tenant_id, message_id)
            if not held:
                raise IntakeInvalid("This mail has nothing held to discard.")
            keys = [item.held_key for item in held if item.held_key]
            for item in held:
                item.outcome, item.held_key = "discarded", None
                item.updated_at = datetime.now(UTC)
            message.status = "discarded"
            message.decided_by, message.decided_at = person[:200], datetime.now(UTC)
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="intake.discarded",
                target_type="intake_message", target_id=str(message_id),
                details={"sender": message.sender, "attachments": len(held)},
            )  # fmt: skip
        for key in keys:
            self._store.delete(key)
        return self.message(tenant_id, message_id)

    @scoped
    def retry(self, tenant_id: uuid.UUID, item_id: uuid.UUID, *, actor: str) -> None:
        """Read again an attachment's document whose reading failed. Raises
        `IntakeNotFound` or `IntakeInvalid`."""
        with self._sessions() as session:
            item = session.scalars(
                select(IntakeItem).where(
                    IntakeItem.tenant_id == tenant_id, IntakeItem.id == item_id
                )
            ).one_or_none()
            if item is None or item.document_id is None:
                raise IntakeNotFound(item_id)
            document_id = item.document_id
        try:
            self._documents.reprocess(tenant_id=tenant_id, document_id=document_id, actor=actor)
        except ReprocessInProgress as error:
            raise IntakeInvalid("It is being read already.") from error

    # --- the address, the list and the settings ------------------------------------------

    @scoped
    def address(self, tenant_id: uuid.UUID) -> AddressView | None:
        """The active address, its token hidden: the whole one was shown when it was made."""
        with self._sessions() as session:
            row = session.scalars(
                select(IntakeAddress).where(
                    IntakeAddress.tenant_id == tenant_id, IntakeAddress.active
                )
            ).one_or_none()
        if row is None:
            return None
        hidden = f"{row.slug}-{'•' * 8}@{self.domain or 'intake.invalid'}"
        return AddressView(row.slug, hidden, row.created_by, row.created_at)

    @scoped
    def make_address(self, tenant_id: uuid.UUID, *, actor: str, person: str) -> AddressView:
        """A new address, shown whole this once (`shown`); the one before stops at once."""
        token = new_token()
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            tenant = session.get(Tenant, tenant_id)
            slug = slug_of((tenant.display_name or tenant.name) if tenant else "workspace")
            old = session.scalars(
                select(IntakeAddress)
                .where(IntakeAddress.tenant_id == tenant_id, IntakeAddress.active)
                .with_for_update()
            ).one_or_none()
            if old is not None:
                old.active, old.revoked_at, old.revoked_by = False, now, person[:200]
                session.flush()
                audit.append(
                    session, tenant_id=tenant_id, actor=actor, action="intake.token_revoked",
                    target_type="intake_address", target_id=str(old.id), details={},
                )  # fmt: skip
            row = IntakeAddress(tenant_id=tenant_id, slug=slug, token_hash=token_hash(token),
                                created_by=person[:200])  # fmt: skip
            session.add(row)
            session.flush()
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="intake.address_created",
                target_type="intake_address", target_id=str(row.id), details={},
            )  # fmt: skip
            session.refresh(row)
            full = f"{row.slug}-{token}@{self.domain or 'intake.invalid'}"
            return AddressView(row.slug, full, row.created_by, row.created_at)

    @staticmethod
    def _settings(session: Session, tenant_id: uuid.UUID) -> SettingsView:
        row = session.get(IntakeSettings, tenant_id)
        if row is None:
            return SettingsView(trust_accepted=False, per_sender_hourly=100)
        return SettingsView(row.trust_accepted, row.per_sender_hourly)

    @scoped
    def settings(self, tenant_id: uuid.UUID) -> SettingsView:
        with self._sessions() as session:
            return self._settings(session, tenant_id)

    @scoped
    def update_settings(
        self, tenant_id: uuid.UUID, changes: dict[str, Any], *, actor: str, person: str
    ) -> SettingsView:
        """Raises `IntakeInvalid`."""
        hourly = changes.get("per_sender_hourly")
        if hourly is not None and not 1 <= int(hourly) <= 10_000:
            raise IntakeInvalid("Documents an hour from one sender is 1 to 10,000.")
        with self._sessions.begin() as session:
            row = session.get(IntakeSettings, tenant_id)
            if row is None:
                row = IntakeSettings(tenant_id=tenant_id, trust_accepted=False,
                                     per_sender_hourly=100)  # fmt: skip
                session.add(row)
            changed = []
            if changes.get("trust_accepted") is not None:
                row.trust_accepted = bool(changes["trust_accepted"])
                changed.append("trust_accepted")
            if hourly is not None:
                row.per_sender_hourly = int(hourly)
                changed.append("per_sender_hourly")
            row.updated_by, row.updated_at = person[:200], datetime.now(UTC)
            session.flush()
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="intake.settings_changed",
                target_type="intake_settings", target_id=str(tenant_id),
                details={"fields": changed},
            )  # fmt: skip
            return SettingsView(row.trust_accepted, row.per_sender_hourly)

    @scoped
    def senders(self, tenant_id: uuid.UUID) -> list[SenderView]:
        with self._sessions() as session:
            rows = session.scalars(
                select(IntakeSender)
                .where(IntakeSender.tenant_id == tenant_id)
                .order_by(IntakeSender.pattern)
            )
            return [SenderView(r.id, r.pattern, r.created_by, r.created_at) for r in rows]

    @scoped
    def add_sender(
        self, tenant_id: uuid.UUID, value: str, *, actor: str, person: str
    ) -> SenderView:
        """Raises `IntakeInvalid`."""
        pattern = pattern_of(value)
        with self._sessions.begin() as session:
            count = session.scalar(select(func.count()).where(IntakeSender.tenant_id == tenant_id))
            if (count or 0) >= MAX_SENDERS:
                raise IntakeInvalid(f"A workspace lists at most {MAX_SENDERS} senders.")
            row = IntakeSender(tenant_id=tenant_id, pattern=pattern, created_by=person[:200])
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                raise IntakeInvalid(f"{pattern} is on the list already.") from error
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="intake.sender_added",
                target_type="intake_sender", target_id=str(row.id),
                details={"sender": pattern},
            )  # fmt: skip
            session.refresh(row)
            return SenderView(row.id, row.pattern, row.created_by, row.created_at)

    @scoped
    def remove_sender(self, tenant_id: uuid.UUID, sender_id: uuid.UUID, *, actor: str) -> None:
        """Raises `IntakeNotFound`."""
        with self._sessions.begin() as session:
            row = session.scalars(
                select(IntakeSender).where(
                    IntakeSender.tenant_id == tenant_id, IntakeSender.id == sender_id
                )
            ).one_or_none()
            if row is None:
                raise IntakeNotFound(sender_id)
            pattern = row.pattern
            session.delete(row)
            audit.append(
                session, tenant_id=tenant_id, actor=actor, action="intake.sender_removed",
                target_type="intake_sender", target_id=str(sender_id),
                details={"sender": pattern},
            )  # fmt: skip

    @scoped
    def test(self, tenant_id: uuid.UUID) -> MessageView:
        """A made-up invoice put through intake as mail from DocForge - the steps after the
        mail provider - so it shows in the Intake list and the documents as a forwarded bill
        would. Raises `NoAddress`."""
        with self._sessions() as session:
            row = session.scalars(
                select(IntakeAddress).where(
                    IntakeAddress.tenant_id == tenant_id, IntakeAddress.active
                )
            ).one_or_none()
            if row is None:
                raise NoAddress(tenant_id)
            address_id = row.id
        email = InboundEmail(
            sender=f"intake-test@{self.domain or 'docforge.invalid'}",
            recipients=(),
            subject="DocForge intake test",
            attachments=(
                InboundAttachment(
                    "docforge-sample-invoice.pdf", "application/pdf", sample_invoice()
                ),
            ),
        )
        return self._deliver(tenant_id, address_id, True, email, trusted=True)
