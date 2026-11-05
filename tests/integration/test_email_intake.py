"""V2 P5: invoices by email. A bill mailed to a workspace's address is read as an upload is,
through the same checks and the same entry point; the sender gets a plain reply; nothing is
ever processed for a sender not on the list, nor lost.

Mail arrives here as an `InboundEmail` (the provider's adapter is tested over HTTP at the
end); replies are caught by a mailer that only records them.
"""

import base64
import io
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry, Document, DocumentVersion
from docforge.db.session import SessionFactory
from docforge.db.tenancy import tenant_scope
from docforge.intake.mailer import Reply
from docforge.intake.model import InboundAttachment, InboundEmail
from docforge.intake.service import (
    NOT_LISTED,
    QUEUE_BUSY,
    REVOKED,
    SMALL_IMAGE,
    STORE_FAILED,
    TEXT_NOTE,
    IntakeService,
    MessageView,
)
from docforge.upload_checks import NOT_ACCEPTED_MESSAGE, too_large_message
from fakes import signed_in
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
ASHA = "asha@supplier.in"
DOMAIN = "in.test"
PASSWORD = "intake-webhook-secret"  # noqa: S105 (a test's)
LABEL = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "pair_001" / "label.json"


class Recorder:
    def __init__(self) -> None:
        self.sent: list[Reply] = []

    def send(self, reply: Reply) -> None:
        self.sent.append(reply)


class Desk:
    def __init__(self, sessions: SessionFactory, world: World) -> None:
        self.sessions, self.world = sessions, world
        self.service = world.build()
        self.mailer = Recorder()
        self.intake = IntakeService(sessions, self.service, world.store, self.mailer,
                                    domain=DOMAIN, web_url="https://app.test")  # fmt: skip
        self.copies = 0

    def address(self, tenant_id: uuid.UUID = DEFAULT_TENANT_ID) -> str:
        return self.intake.make_address(tenant_id, actor="user:admin", person="Admin").shown

    def allow(self, sender: str = ASHA, tenant_id: uuid.UUID = DEFAULT_TENANT_ID) -> None:
        self.intake.add_sender(tenant_id, sender, actor="user:admin", person="Admin")

    def pdf(self) -> bytes:
        """pair_001's invoice, as new bytes each time (not to be read: the parser knows only
        the original's)."""
        self.copies += 1
        return bytes(self.world.invoice_pdf) + f"\n%mailed copy {self.copies}\n".encode()

    def original(self) -> bytes:
        """pair_001's invoice as the parser knows it, to be read to the end."""
        return bytes(self.world.invoice_pdf)

    def mail(self, to: str, *files: tuple[str, bytes], sender: str = ASHA,
             subject: str = "Bill") -> list[MessageView]:  # fmt: skip
        attachments = tuple(InboundAttachment(name, None, data) for name, data in files)
        return self.intake.receive(
            InboundEmail(sender=sender, recipients=(to,), subject=subject,
                         message_id=f"<{uuid.uuid4()}@mail.test>", attachments=attachments)
        )  # fmt: skip

    def documents(self, tenant_id: uuid.UUID = DEFAULT_TENANT_ID) -> list[Document]:
        with tenant_scope(tenant_id), self.sessions() as session:
            return list(session.scalars(select(Document).where(Document.tenant_id == tenant_id)))

    def process(self, document_id: uuid.UUID) -> str:
        with self.sessions() as session:
            version = session.scalars(
                select(DocumentVersion.id).where(DocumentVersion.document_id == document_id)
            ).one()
        return str(self.service.process(version))

    def audit(self, prefix: str = "intake.") -> list[AuditEntry]:
        with self.sessions() as session:
            return list(session.scalars(
                select(AuditEntry).where(AuditEntry.action.startswith(prefix))
                .order_by(AuditEntry.id)
            ))  # fmt: skip


@pytest.fixture
def desk(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Desk:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    return Desk(sessions, world)


def _png(size: int = 8) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), "white").save(buffer, format="PNG")
    return buffer.getvalue()


# AC-P5-1: one invoice, one document, read to the end


def test_a_mailed_invoice_is_one_document_read_to_the_end(desk: Desk) -> None:
    to = desk.address()
    desk.allow()

    (message,) = desk.mail(to, ("bill.pdf", desk.original()))

    (item,) = message.items
    assert (message.status, item.outcome) == ("received", "received")
    (document,) = desk.documents()
    assert (document.id, document.source, document.filename) == (
        item.document_id, "email", "bill.pdf"
    )  # fmt: skip
    assert desk.process(document.id) == "succeeded"
    (reply,) = desk.mailer.sent
    assert reply.to == ASHA and reply.subject == "Received: Bill"
    assert "- bill.pdf: received, being read." in reply.body
    assert "https://app.test/documents?source=email" in reply.body


# AC-P5-2: the same bytes from any channel are one document


def test_the_same_bytes_mailed_then_uploaded_are_one_document(desk: Desk) -> None:
    to = desk.address()
    desk.allow()
    data = desk.pdf()

    (first,) = desk.mail(to, ("bill.pdf", data))
    uploaded = desk.service.ingest(tenant_id=DEFAULT_TENANT_ID, doc_type="invoice",
                                   filename="bill.pdf", data=data, actor="api:upload")  # fmt: skip
    (again,) = desk.mail(to, ("bill-again.pdf", data))

    assert not uploaded.created and uploaded.document.id == first.items[0].document_id
    assert again.items[0].outcome == "already_received"
    assert again.items[0].document_id == first.items[0].document_id
    assert len(desk.documents()) == 1
    assert "already received earlier, so nothing new was made" in desk.mailer.sent[-1].body


# AC-P5-3: a sender not on the list is held; accept reads it, discard deletes it


def test_mail_from_a_sender_not_listed_is_held_until_a_reviewer_accepts_it(desk: Desk) -> None:
    to = desk.address()

    (held,) = desk.mail(to, ("bill.pdf", desk.pdf()), sender="stranger@else.in")

    assert (held.status, held.reason) == ("held", NOT_LISTED)
    assert held.items[0].outcome == "held" and desk.documents() == []
    assert desk.mailer.sent == []  # no reply to a sender who may be forged
    accepted = desk.intake.accept(DEFAULT_TENANT_ID, held.id, actor="user:rev", person="Rev")
    assert accepted.status == "accepted" and accepted.items[0].outcome == "received"
    assert [d.source for d in desk.documents()] == ["email"]
    actions = [(e.action, e.details["sender"]) for e in desk.audit()
               if e.action in ("intake.held", "intake.accepted")]  # fmt: skip
    assert actions == [
        ("intake.held", "stranger@else.in"), ("intake.accepted", "stranger@else.in")
    ]  # fmt: skip


def test_discarding_held_mail_deletes_its_copies_and_reads_nothing(desk: Desk) -> None:
    to = desk.address()
    (held,) = desk.mail(to, ("bill.pdf", desk.pdf()), sender="stranger@else.in")
    stored = desk.world.store.keys()  # a store's keys, not a dict's
    keys = [k for k in stored if k.startswith(f"intake/{DEFAULT_TENANT_ID}/")]

    discarded = desk.intake.discard(DEFAULT_TENANT_ID, held.id, actor="user:rev", person="Rev")

    assert len(keys) == 1
    assert discarded.status == "discarded" and discarded.items[0].outcome == "discarded"
    left = desk.world.store.keys()
    assert not any(k.startswith("intake/") for k in left)
    assert desk.documents() == []
    details = desk.audit("intake.discarded")[0].details
    assert details == {"sender": "stranger@else.in", "attachments": 1}


def test_trusting_a_sender_once_accepted_is_off_unless_turned_on(desk: Desk) -> None:
    to = desk.address()
    (first,) = desk.mail(to, ("a.pdf", desk.pdf()), sender="new@supplier.in")
    desk.intake.accept(DEFAULT_TENANT_ID, first.id, actor="user:rev", person="Rev")
    assert desk.intake.senders(DEFAULT_TENANT_ID) == []

    desk.intake.update_settings(DEFAULT_TENANT_ID, {"trust_accepted": True}, actor="user:a",
                                person="Admin")  # fmt: skip
    (second,) = desk.mail(to, ("b.pdf", desk.pdf()), sender="new@supplier.in")
    desk.intake.accept(DEFAULT_TENANT_ID, second.id, actor="user:rev", person="Rev")
    (third,) = desk.mail(to, ("c.pdf", desk.pdf()), sender="new@supplier.in")

    assert [s.pattern for s in desk.intake.senders(DEFAULT_TENANT_ID)] == ["new@supplier.in"]
    assert third.status == "received"


def test_a_domain_on_the_list_lets_everyone_at_it_through(desk: Desk) -> None:
    to = desk.address()
    desk.allow("@supplier.in")

    (message,) = desk.mail(to, ("bill.pdf", desk.pdf()), sender="Accounts@Supplier.in")

    assert message.status == "received" and message.sender == "accounts@supplier.in"


# AC-P5-4: a revoked address makes nothing; the new one works


def test_mail_to_a_revoked_address_makes_nothing_and_the_new_one_works(desk: Desk) -> None:
    old = desk.address()
    desk.allow()
    new = desk.address()  # regenerating revokes the old one

    (rejected,) = desk.mail(old, ("bill.pdf", desk.pdf()))
    (taken,) = desk.mail(new, ("bill.pdf", desk.pdf()))

    assert (rejected.status, rejected.reason, rejected.items) == ("rejected", REVOKED, ())
    assert taken.status == "received" and len(desk.documents()) == 1
    assert len(desk.mailer.sent) == 1  # only the mail to the new address is answered
    actions = [e.action for e in desk.audit() if "token" in e.action or "address" in e.action]
    assert actions == ["intake.address_created", "intake.token_revoked",
                       "intake.address_created"]  # fmt: skip


def test_an_address_shows_whole_once_and_hidden_after(desk: Desk) -> None:
    made = desk.address()
    later = desk.intake.address(DEFAULT_TENANT_ID)

    slug, token = made.split("@")[0].rsplit("-", 1)
    assert len(token) == 26 and made.endswith(f"@{DOMAIN}")
    assert later is not None and token not in later.shown and later.shown.startswith(slug)


# AC-P5-5: logos and notes ignored, with reasons; the invoice read; the reply lists all three


def test_an_invoice_with_a_logo_and_a_note_reads_the_invoice_only(desk: Desk) -> None:
    to = desk.address()
    desk.allow()

    (message,) = desk.mail(to, ("bill.pdf", desk.pdf()), ("logo.png", _png()),
                           ("note.txt", b"Please pay by Friday."))  # fmt: skip

    outcomes = [(i.filename, i.outcome, i.reason) for i in message.items]
    assert outcomes == [
        ("bill.pdf", "received", None), ("logo.png", "ignored", SMALL_IMAGE),
        ("note.txt", "ignored", TEXT_NOTE),
    ]  # fmt: skip
    assert len(desk.documents()) == 1
    body = desk.mailer.sent[0].body
    assert "- bill.pdf: received, being read." in body
    assert f"- logo.png: not read: {SMALL_IMAGE}." in body
    assert f"- note.txt: not read: {TEXT_NOTE}." in body


# AC-P5-6: too large or not accepted: refused as an upload is, the rest still read


def test_a_file_too_large_or_not_accepted_is_refused_as_an_upload_is(desk: Desk) -> None:
    to = desk.address()
    desk.allow()
    huge = b"%PDF-1.4\n" + b"0" * (10 * 1024 * 1024)

    (message,) = desk.mail(to, ("huge.pdf", huge), ("tool.exe", b"MZ\x90\x00" * 4000),
                           ("bill.pdf", desk.pdf()))  # fmt: skip

    reasons = {i.filename: (i.outcome, i.reason) for i in message.items}
    assert reasons["huge.pdf"] == ("refused", too_large_message(10 * 1024 * 1024))
    assert reasons["tool.exe"] == ("refused", NOT_ACCEPTED_MESSAGE)
    assert reasons["bill.pdf"] == ("received", None)
    assert len(desk.documents()) == 1


# AC-P5-7: no value read from a bill in the reply


def test_the_reply_carries_no_value_from_the_bill(desk: Desk) -> None:
    to = desk.address()
    desk.allow()
    (message,) = desk.mail(to, ("bill.pdf", desk.original()))
    document_id = message.items[0].document_id
    assert document_id is not None and desk.process(document_id) == "succeeded"
    label = json.loads(LABEL.read_text())["invoice"]
    values = [label["seller"]["gstin"], label["buyer"]["gstin"], label["invoice_no"],
              label["totals"]["grand_total"], label["totals"]["taxable_value"]]  # fmt: skip

    reply = desk.mailer.sent[0]

    assert all(str(v) not in reply.body + reply.subject for v in values if v), values


# AC-P5-8: an address only ever reaches its own workspace


def test_an_address_only_reaches_its_own_workspace(desk: Desk, other_tenant: uuid.UUID) -> None:
    mine = desk.address()
    theirs = desk.address(other_tenant)
    desk.allow()
    desk.allow(tenant_id=other_tenant)
    my_slug, my_token = mine.split("@")[0].rsplit("-", 1)
    their_slug, their_token = theirs.split("@")[0].rsplit("-", 1)

    desk.mail(f"{my_slug}-{their_token}@{DOMAIN}", ("a.pdf", desk.pdf()))  # their token
    desk.mail(f"{their_slug}-{my_token}@{DOMAIN}", ("b.pdf", desk.pdf()))  # my token
    desk.mail(f"{my_slug}-{'a' * 26}@{DOMAIN}", ("c.pdf", desk.pdf()))  # no one's

    assert [d.filename for d in desk.documents(other_tenant)] == ["a.pdf"]
    assert [d.filename for d in desk.documents()] == ["b.pdf"]
    assert len(desk.intake.messages(DEFAULT_TENANT_ID)) == 1
    assert len(desk.intake.messages(other_tenant)) == 1


# Limits, failures, the Intake list


def test_past_a_senders_hourly_limit_attachments_are_held_with_the_reason(desk: Desk) -> None:
    to = desk.address()
    desk.allow()
    desk.intake.update_settings(DEFAULT_TENANT_ID, {"per_sender_hourly": 2}, actor="user:a",
                                person="Admin")  # fmt: skip

    (message,) = desk.mail(to, *[(f"{n}.pdf", desk.pdf()) for n in range(3)])

    assert [i.outcome for i in message.items] == ["received", "received", "held"]
    assert message.items[2].reason == "rate limit: 2 documents an hour from one sender"
    assert [m.id for m in desk.intake.messages(DEFAULT_TENANT_ID, view="held")] == [message.id]


def test_uploads_keep_the_last_places_in_the_queue(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    to = desk.address()
    desk.allow()
    monkeypatch.setattr(type(desk.service), "max_pending", property(lambda _: 101))

    (first,) = desk.mail(to, ("a.pdf", desk.pdf()))
    (second,) = desk.mail(to, ("b.pdf", desk.pdf()))
    upload = desk.service.ingest(
        tenant_id=DEFAULT_TENANT_ID, doc_type="invoice", filename="up.pdf", data=desk.pdf(),
        actor="api:upload",
    )  # fmt: skip

    assert first.items[0].outcome == "received"
    assert (second.items[0].outcome, second.items[0].reason) == ("held", QUEUE_BUSY)
    assert upload.created


def test_an_attachment_that_breaks_cannot_stop_the_rest_nor_later_mail(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    to = desk.address()
    desk.allow()
    real = desk.service.ingest

    def poison(**kwargs: Any) -> Any:
        if kwargs["filename"] == "poison.pdf":
            raise RuntimeError("storage fell over")
        return real(**kwargs)

    monkeypatch.setattr(desk.service, "ingest", poison)

    (message,) = desk.mail(to, ("poison.pdf", desk.pdf()), ("bill.pdf", desk.pdf()))
    (later,) = desk.mail(to, ("next.pdf", desk.pdf()))

    assert [(i.outcome, i.reason) for i in message.items] == [
        ("refused", STORE_FAILED), ("received", None)
    ]  # fmt: skip
    assert later.items[0].outcome == "received"


def test_a_failed_reading_shows_in_the_intake_list_and_can_be_retried(
    desk: Desk, owner_engine: Engine
) -> None:
    to = desk.address()
    desk.allow()
    (message,) = desk.mail(to, ("bill.pdf", desk.original()))
    document_id = message.items[0].document_id
    assert document_id is not None and desk.process(document_id) == "succeeded"
    with owner_engine.begin() as conn:  # as if its reading had failed for good
        conn.execute(text("UPDATE documents SET status = 'failed' WHERE id = :d"),
                     {"d": document_id})  # fmt: skip

    failed = desk.intake.messages(DEFAULT_TENANT_ID, view="failed")
    desk.intake.retry(DEFAULT_TENANT_ID, message.items[0].id, actor="user:rev")

    assert [m.id for m in failed] == [message.id]
    with desk.sessions() as session:
        versions = session.scalars(
            select(DocumentVersion.version_no).where(DocumentVersion.document_id == document_id)
        ).all()
    assert sorted(versions) == [1, 2]


def test_the_test_button_puts_a_sample_invoice_through_intake(desk: Desk) -> None:
    desk.address()

    message = desk.intake.test(DEFAULT_TENANT_ID)

    assert message.status == "received" and message.items[0].outcome == "received"
    assert [d.source for d in desk.documents()] == ["email"]
    assert desk.mailer.sent == []  # no reply to DocForge itself


# Over HTTP: the provider's routes, the settings and the Intake list


def _client(desk: Desk, role: str = "admin", password: str | None = PASSWORD) -> TestClient:
    app = create_app(None, service=desk.service, intake=desk.intake,
                     intake_webhook_password=password)  # fmt: skip
    return TestClient(signed_in(app, role=role))


def _basic(password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"intake:{password}".encode()).decode()}


def test_postmarks_webhook_delivers_with_the_password_and_only_with_it(desk: Desk) -> None:
    to = desk.address()
    desk.allow()
    payload = {
        "FromFull": {"Email": ASHA, "Name": "Asha"}, "ToFull": [{"Email": to}],
        "OriginalRecipient": to, "Subject": "Bill", "MessageID": "abc-1",
        "Attachments": [{"Name": "bill.pdf", "ContentType": "application/pdf",
                         "Content": base64.b64encode(desk.pdf()).decode(), "ContentID": ""}],
    }  # fmt: skip
    client = _client(desk)
    route = "/v1/intake/inbound/postmark"

    assert client.post(route, json=payload).status_code == 401
    assert client.post(route, json=payload, headers=_basic("wrong")).status_code == 401
    assert _client(desk, password=None).post(route, json=payload).status_code == 503
    got = client.post(route, json=payload, headers=_basic(PASSWORD))

    assert got.status_code == 200 and got.json() == {"delivered": 1}
    assert [d.source for d in desk.documents()] == ["email"]


def test_the_provider_agnostic_route_takes_a_parsed_mail(desk: Desk) -> None:
    to = desk.address()
    desk.allow()
    body = {"from": f"Asha <{ASHA}>", "to": [to], "subject": "Bill",
            "attachments": [{"filename": "bill.pdf",
                             "content": base64.b64encode(desk.pdf()).decode()}]}  # fmt: skip

    got = _client(desk).post("/v1/intake/inbound", json=body, headers=_basic(PASSWORD))

    assert got.json() == {"delivered": 1}
    member = _client(desk, role="member")
    emailed = member.get("/v1/documents", params={"source": "email"}).json()["items"]
    assert [d["source"] for d in emailed] == ["email"]
    assert member.get("/v1/documents", params={"source": "upload"}).json()["items"] == []


def test_settings_are_for_admins_and_the_intake_list_for_everyone_who_reads(desk: Desk) -> None:
    admin, reviewer = _client(desk), _client(desk, role="reviewer")

    made = admin.post("/v1/settings/intake/address")
    listed = admin.post("/v1/settings/intake/senders", json={"sender": "@Supplier.in"})
    bad = admin.post("/v1/settings/intake/senders", json={"sender": "not an address"})
    shown = admin.get("/v1/settings/intake").json()

    assert made.status_code == 201 and made.json()["address"]["whole"] is True
    assert listed.status_code == 201 and listed.json()["sender"] == "@supplier.in"
    assert bad.status_code == 422 and "neither an email address" in bad.json()["detail"]
    assert shown["address"]["whole"] is False and shown["enabled"] is True
    assert reviewer.post("/v1/settings/intake/address").status_code == 403
    assert reviewer.get("/v1/settings/intake").status_code == 403
    assert reviewer.get("/v1/intake/messages").status_code == 200
    tested = admin.post("/v1/settings/intake/test")
    assert tested.status_code == 200 and tested.json()["items"][0]["outcome"] == "received"
