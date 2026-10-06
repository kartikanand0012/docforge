"""Keys for AI agents, made by administrators, and what agents have called.

The web makes only `reader` keys: an agent's key can read, never upload, review or administer.
The token is shown once; only a hash of its secret is kept. Other roles stay with
`python -m docforge.admin create-key`.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from docforge.api.auth import require
from docforge.auth import Authenticator, Principal
from docforge.mcp_server.tools import AgentTools

Admin = Annotated[Principal, Depends(require("admin"))]
# Each key an agent holds is a way in: few enough to know them all.
MAX_AGENT_KEYS = 20


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="What the key is for")


class KeyMade(BaseModel):
    token: str
    prefix: str
    name: str
    role: str


class KeyOut(BaseModel):
    prefix: str
    name: str
    role: str
    created_by: str | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class AgentCallOut(BaseModel):
    tool: str
    outcome: str
    key_name: str
    scope: str
    results: int
    duration_ms: int
    created_at: datetime


def agents_router(authenticator: Authenticator, tools: AgentTools) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/api-keys", response_model=list[KeyOut])
    async def keys(principal: Admin) -> list[KeyOut]:
        found = await run_in_threadpool(authenticator.api_keys, principal.tenant_id)
        return [
            KeyOut(
                prefix=k.prefix,
                name=k.name,
                role=k.role,
                created_by=k.created_by,
                created_at=k.created_at,
                last_used_at=k.last_used_at,
                revoked_at=k.revoked_at,
            )
            for k in found
        ]

    @router.post("/api-keys", response_model=KeyMade, status_code=201)
    async def make(body: KeyIn, principal: Admin, response: Response) -> KeyMade:
        """A read-only key for an AI agent. Its token is in this reply only."""
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "A key needs a name.")
        found = await run_in_threadpool(authenticator.api_keys, principal.tenant_id)
        active = [k for k in found if k.role == "reader" and k.revoked_at is None]
        if len(active) >= MAX_AGENT_KEYS:
            raise HTTPException(
                409, f"An organisation has at most {MAX_AGENT_KEYS} keys for agents. "
                "Revoke one no longer used first.",
            )  # fmt: skip
        token = await run_in_threadpool(
            lambda: authenticator.create_api_key(
                principal.tenant_id, name=name, role="reader", actor=principal.actor
            )
        )
        response.headers["Cache-Control"] = "no-store"
        return KeyMade(token=token, prefix=token.split("_")[1], name=name, role="reader")

    @router.delete("/api-keys/{prefix}", status_code=204)
    async def revoke(prefix: str, principal: Admin) -> Response:
        try:
            await run_in_threadpool(
                authenticator.revoke_api_key, principal.tenant_id, prefix, principal.actor
            )
        except LookupError:
            raise HTTPException(404, "Not found.") from None
        return Response(status_code=204)

    @router.get("/agent-calls", response_model=list[AgentCallOut])
    async def calls(principal: Admin) -> list[AgentCallOut]:
        found = await run_in_threadpool(tools.calls, principal.tenant_id)
        return [
            AgentCallOut(
                tool=c.tool,
                outcome=c.outcome,
                key_name=c.key_name,
                scope=c.scope,
                results=c.results,
                duration_ms=c.duration_ms,
                created_at=c.created_at,
            )
            for c in found
        ]

    return router
