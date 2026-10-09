"""The whole API over the real services, with sign-up on: for tests of who may reach what.

Documents are read by a world's scripted parser and model (`worlds.World`), into whichever
workspace a test names, and indexed for search at once.
"""

import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from test_chat import Model

from docforge.api.app import MemberCaps, create_app
from docforge.audit_log import AuditLogService
from docforge.auth import Authenticator
from docforge.chat.service import ChatService
from docforge.collections import CollectionService
from docforge.db.session import SessionFactory
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.limits import LocalLimits
from docforge.mcp_server.tools import AgentTools
from docforge.review.service import ReviewService
from docforge.search.embeddings import FakeEmbedder
from docforge.search.service import SearchService
from docforge.webhooks import WebhookService
from worlds import World

PASSWORD = "correct horse battery staple"  # noqa: S105 - a test account's


class FullStack:
    def __init__(
        self,
        sessions: SessionFactory,
        world: World,
        *,
        signup: bool = True,
        signups_per_day: int = 200,
        caps: MemberCaps | None = None,
    ) -> None:
        self.sessions, self.world = sessions, world
        self.documents = world.service or world.build()
        self.search = SearchService(sessions, FakeEmbedder())
        self.model = Model()
        self.chat = ChatService(sessions, self.search, self.model, daily_limit=500)
        self.collections = CollectionService(sessions)
        self.review = ReviewService(
            sessions, world.store, {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC}
        )
        self.auth = Authenticator(sessions, failed_logins_per_window=1000)
        self.limits = LocalLimits()
        self.webhooks = WebhookService(sessions, b"k" * 32, lambda *a: None, allow_http=True)
        self.tools = AgentTools(
            sessions, search=self.search, chat=self.chat, collections=self.collections,
            documents=self.documents, limits=self.limits,
        )  # fmt: skip
        self.app = create_app(
            None, service=self.documents, review=self.review, authenticator=self.auth,
            webhooks=self.webhooks, search=self.search, chat=self.chat,
            collections=self.collections, limits=self.limits, agents=self.tools,
            audit_log=AuditLogService(sessions), signup_enabled=signup,
            signups_per_day=signups_per_day, member_caps=caps or MemberCaps(),
        )  # fmt: skip
        self.client = TestClient(self.app)

    # People

    def sign_up(self, name: str, email: str, password: str = PASSWORD) -> dict[str, str]:
        response = self.client.post(
            "/v1/accounts", json={"name": name, "email": email, "password": password}
        )
        assert response.status_code == 201, response.text
        return {"Authorization": f"Bearer {response.json()['token']}"}

    def sign_in(
        self, email: str, secret: str, tenant: str | None = None, *, field: str = "password"
    ) -> dict[str, str]:
        body: dict[str, Any] = {"email": email, field: secret}
        if tenant is not None:
            body["tenant"] = tenant
        response = self.client.post("/v1/sessions", json=body)
        assert response.status_code == 201, response.text
        return {"Authorization": f"Bearer {response.json()['token']}"}

    def key(self, tenant_id: uuid.UUID, role: str) -> str:
        return self.auth.create_api_key(tenant_id, name=f"{role} key", role=role)

    @staticmethod
    def workspace_of(owner_engine: Engine, email: str) -> uuid.UUID:
        with owner_engine.connect() as conn:
            found: uuid.UUID = conn.execute(
                text("SELECT tenant_id FROM accounts WHERE email = :e"), {"e": email}
            ).scalar_one()
        return found

    # Documents

    def read(self, tenant_id: uuid.UUID, doc_type: str = "invoice") -> uuid.UUID:
        """A document read into `tenant_id` and indexed, as the worker would."""
        data = self.world.invoice_pdf if doc_type == "invoice" else self.world.order_pdf
        result = self.documents.ingest(
            tenant_id=tenant_id, doc_type=doc_type, filename=f"{doc_type}.pdf", data=data,
            actor="api:upload",
        )  # fmt: skip
        if result.version is not None:
            assert self.documents.process(result.version.id) == "succeeded"
        self.search.index_document(tenant_id, result.document.id)
        return result.document.id
