"""The MCP server on the API, at `/v1/mcp`: Streamable HTTP, stateless, with JSON replies.

Every API process answers any call, nothing is kept between requests, and a revoked key fails
on its next call. Before any JSON-RPC is read, the request is checked here: a Bearer token for
a `reader` API key (401 without one, 403 for any other key or a person's session), an Origin
the site allows (against DNS rebinding), and a body of at most 64 KB. The caller then rides on
the request to the tool that runs, never in anything shared.
"""

import contextlib
import functools
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import anyio
from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from docforge.auth import Authenticator, Principal
from docforge.mcp_server.tools import NOTICE, AgentTools, ToolFailure
from docforge.search.service import Mode

MAX_BODY_BYTES = 64 * 1024
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
INSTRUCTIONS = (
    "DocForge holds this organisation's documents: invoices, purchase orders, certificates of "
    "analysis and general documents, grouped into knowledge bases. Use search_documents to "
    "find passages, ask for a checked answer with its quotes, and get_document or "
    f"get_page_text to read one. {NOTICE}"
)

Ctx = Context[Any, Any, Any]
Id = Annotated[str, Field(description="An id from another tool's result (a UUID).")]
OptionalId = Annotated[str | None, Field(description="An id (a UUID), if any.")]


def _result(text: str, *, error: bool) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=error)


def build_server(tools: AgentTools) -> FastMCP:
    mcp = FastMCP(
        "DocForge",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        # The endpoint checks the Origin itself, against the site's own origins.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def run(
        ctx: Ctx, tool: str, ids: dict[str, str | None], **arguments: Any
    ) -> CallToolResult:
        """Run `tool` as the request's caller, in a thread: the services block."""
        try:
            parsed = {name: None if v is None else uuid.UUID(v) for name, v in ids.items()}
        except ValueError:
            return _result("An id is not one DocForge gave: it must be a UUID.", error=True)
        request = ctx.request_context.request
        if request is None:  # only over HTTP, where the endpoint put the caller
            return _result("Internal error.", error=True)
        principal: Principal = request.state.principal
        call = functools.partial(tools.run, principal, tool, **parsed, **arguments)
        try:
            result = await anyio.to_thread.run_sync(call)
        except ToolFailure as failure:
            return _result(str(failure), error=True)
        return _result(json.dumps(result, ensure_ascii=False, indent=1), error=False)

    @mcp.tool(annotations=READ_ONLY)
    async def list_knowledge_bases(ctx: Ctx) -> CallToolResult:
        """The organisation's knowledge bases: named groups of its documents, with their size."""
        return await run(ctx, "list_knowledge_bases", {})

    @mcp.tool(annotations=READ_ONLY)
    async def list_documents(
        ctx: Ctx,
        knowledge_base_id: OptionalId = None,
        doc_type: Annotated[
            str | None, Field(description="invoice, purchase_order, coa or general")
        ] = None,
        stage: Annotated[str | None, Field(description="ready: can be asked about")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        cursor: Annotated[str | None, Field(description="next_cursor of the last page")] = None,
    ) -> CallToolResult:
        """The organisation's documents, newest first, or a knowledge base's."""
        return await run(
            ctx,
            "list_documents",
            {"knowledge_base_id": knowledge_base_id},
            doc_type=doc_type,
            stage=stage,
            limit=limit,
            cursor=cursor,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def search_documents(
        ctx: Ctx,
        query: Annotated[str, Field(min_length=1, max_length=500)],
        k: Annotated[int, Field(ge=1, le=20)] = 8,
        mode: Mode = "hybrid",
        doc_type: str | None = None,
        document_id: OptionalId = None,
        knowledge_base_id: OptionalId = None,
    ) -> CallToolResult:
        """Passages of the organisation's documents that match `query`, best first, each with
        its document and page. Within one document or one knowledge base if given."""
        return await run(
            ctx,
            "search_documents",
            {"document_id": document_id, "knowledge_base_id": knowledge_base_id},
            query=query,
            k=k,
            mode=mode,
            doc_type=doc_type,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def ask(
        ctx: Ctx,
        question: Annotated[str, Field(min_length=1, max_length=2000)],
        document_id: OptionalId = None,
        knowledge_base_id: OptionalId = None,
        conversation_id: Annotated[
            str | None, Field(description="To follow up: the conversation_id of an answer.")
        ] = None,
    ) -> CallToolResult:
        """A checked answer from the organisation's documents: every statement rests on a
        quote found in them, with its document and page. When they do not say, it says so,
        and what was missing."""
        return await run(
            ctx,
            "ask",
            {
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "conversation_id": conversation_id,
            },
            question=question,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_document(ctx: Ctx, document_id: Id) -> CallToolResult:
        """One document: its type, stage, pages, and the fields read from it."""
        return await run(ctx, "get_document", {"document_id": document_id})

    @mcp.tool(annotations=READ_ONLY)
    async def get_page_text(
        ctx: Ctx, document_id: Id, page: Annotated[int, Field(ge=1)]
    ) -> CallToolResult:
        """The text of one page of a document, in reading order."""
        return await run(ctx, "get_page_text", {"document_id": document_id}, page=page)

    mcp.streamable_http_app()  # creates the session manager the endpoint uses
    return mcp


async def _reply(send: Send, status: int, detail: str, **headers: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    sent = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    sent += [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    await send({"type": "http.response.start", "status": status, "headers": sent})
    await send({"type": "http.response.body", "body": body})


class McpEndpoint:
    """The checks before the SDK, as an ASGI app."""

    def __init__(self, mcp: FastMCP, authenticator: Authenticator, origins: list[str]) -> None:
        self._mcp = mcp
        self._authenticator = authenticator
        self._origins = set(origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        origin = request.headers.get("origin")
        if origin is not None and origin not in self._origins:
            await _reply(send, 403, "This origin may not call DocForge.")
            return
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        principal = None
        if scheme.lower() == "bearer" and token:
            principal = await anyio.to_thread.run_sync(self._authenticator.authenticate, token)
        if principal is None:
            await _reply(
                send, 401, "A read-only API key is needed.", **{"WWW-Authenticate": "Bearer"}
            )
            return
        if principal.kind != "api_key" or principal.role != "reader":
            await _reply(
                send,
                403,
                "AI agents use a read-only key: an administrator makes one under "
                "Connect an AI agent.",
            )
            return
        if request.method != "POST":
            await _reply(send, 405, "Send MCP requests by POST.", Allow="POST")
            return
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            await _reply(send, 413, "The request is too large.")
            return
        body = bytearray()
        while True:
            message = await receive()
            body += message.get("body", b"")
            if len(body) > MAX_BODY_BYTES:
                await _reply(send, 413, "The request is too large.")
                return
            if not message.get("more_body", False):
                break
        given = False

        async def replay() -> Message:
            nonlocal given
            if given:
                return await receive()  # a disconnect, once the body is read
            given = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        scope.setdefault("state", {})["principal"] = principal
        await self._mcp.session_manager.handle_request(scope, replay, send)


def mount(
    app: FastAPI, tools: AgentTools, authenticator: Authenticator, origins: list[str]
) -> None:
    """`/v1/mcp` on `app`, its session manager running for the app's life."""
    mcp = build_server(tools)
    app.router.routes.append(Route("/v1/mcp", endpoint=McpEndpoint(mcp, authenticator, origins)))
    before = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(started: Any) -> AsyncIterator[Any]:
        async with mcp.session_manager.run(), before(started) as state:
            yield state

    app.router.lifespan_context = lifespan
