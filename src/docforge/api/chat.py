"""Chat endpoints: ask about the organisation's documents (or one of them), and read back
one's own conversations."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from docforge.api.auth import require
from docforge.auth import Principal
from docforge.chat.service import (
    ChatService,
    ConversationNotFound,
    DocumentNotFound,
    QuestionLimitReached,
    ScopeConflict,
    ScopeGone,
)
from docforge.collections import CollectionNotFound
from docforge.limits import LimitReached, Limits
from docforge.llm.base import LLMError

logger = logging.getLogger(__name__)
Reader = Annotated[Principal, Depends(require("documents:read"))]
MAX_IN_FLIGHT_PER_CALLER = 2
_running: set[asyncio.Task[None]] = set()
_UNAVAILABLE = "The model could not answer just now. Try again shortly."


class QuestionIn(BaseModel):
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    document_id: uuid.UUID | None = None
    collection_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("the question is empty")
        return value


class CitationOut(BaseModel):
    document_id: uuid.UUID
    filename: str
    doc_type: str
    page: int
    quote: str
    boxes: list[dict[str, Any]]


class AnswerOut(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    # supported, partly_supported (some quotes dropped), unsupported (withheld), not_found
    status: str
    text: str
    citations: list[CitationOut]
    dropped_citations: int
    words_only: bool


class MessageOut(BaseModel):
    id: uuid.UUID
    question: str
    answer: str
    status: str
    citations: list[CitationOut]
    created_at: datetime


class ConversationOut(BaseModel):
    id: uuid.UUID
    messages: list[MessageOut]


class ConversationSummaryOut(BaseModel):
    id: uuid.UUID
    title: str
    document_id: uuid.UUID | None
    created_at: datetime


def _http_error(error: Exception) -> HTTPException:
    """What a failed question means to the caller."""
    if isinstance(error, QuestionLimitReached):
        return HTTPException(429, "The organisation's questions for today are used up.")
    if isinstance(error, ConversationNotFound | DocumentNotFound | CollectionNotFound):
        return HTTPException(404, "Not found.")
    if isinstance(error, ScopeConflict):
        return HTTPException(
            422, "A question is about one document or one knowledge base, and a conversation "
            "keeps the one it began with; start a new conversation.",
        )  # fmt: skip
    if isinstance(error, ScopeGone):
        return HTTPException(
            409, "The document or knowledge base this conversation was about has been deleted."
        )
    if isinstance(error, LLMError):
        return HTTPException(503, _UNAVAILABLE)
    logger.exception("chat failed")
    return HTTPException(500, "Internal error.")


def _answer_out(answer: Any) -> "AnswerOut":
    return AnswerOut(
        conversation_id=answer.conversation_id,
        message_id=answer.message_id,
        status=answer.status,
        text=answer.text,
        citations=[CitationOut(**c.as_json()) for c in answer.citations],
        dropped_citations=answer.dropped_citations,
        words_only=answer.words_only,
    )


def chat_router(chat: ChatService, per_minute: int, limits: Limits) -> APIRouter:
    router = APIRouter(prefix="/v1")

    def minute(principal: Principal) -> bool:
        # Each question is a paid model call: counted per caller and minute, everywhere.
        return limits.allow(f"chat-minute:{principal.tenant_id}:{principal.actor}", per_minute)

    def slot(principal: Principal) -> AbstractContextManager[None]:
        # Each question holds a thread for its model call: a few at a time per caller, across
        # every API process. A place left by a process that died frees itself after 15 min.
        key = f"chat:{principal.tenant_id}:{principal.actor}"
        return limits.hold(key, MAX_IN_FLIGHT_PER_CALLER, 900)

    async def take(principal: Principal) -> AbstractContextManager[None]:
        if not await run_in_threadpool(minute, principal):
            raise HTTPException(429, "Too many questions; wait a minute.")
        held = slot(principal)
        try:
            await run_in_threadpool(held.__enter__)
        except LimitReached:
            raise HTTPException(429, "Wait for your other questions to be answered.") from None
        return held

    def call(body: QuestionIn, principal: Principal, progress: Any = None) -> Any:
        return chat.ask(
            principal.tenant_id,
            principal.actor,
            body.question,
            document_id=body.document_id,
            collection_id=body.collection_id,
            conversation_id=body.conversation_id,
            progress=progress,
        )

    @router.post("/chat", response_model=AnswerOut)
    async def ask(body: QuestionIn, principal: Reader) -> AnswerOut:
        held = await take(principal)
        try:
            answer = await run_in_threadpool(call, body, principal)
        except Exception as error:
            raise _http_error(error) from error
        finally:
            await run_in_threadpool(held.__exit__, None, None, None)
        return _answer_out(answer)

    @router.post("/chat/stream")
    async def ask_streamed(body: QuestionIn, principal: Reader) -> StreamingResponse:
        """Server-sent events: a `stage` event as each stage begins (searching, reading with
        the number of passages, checking), then the checked `answer`, or an `error`. The
        answer's text is never streamed before its quotes are checked."""
        held = await take(
            principal
        )  # released when the answer is in, whether or not anyone listens
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

        def tell(stage: str, details: dict[str, Any]) -> None:
            # The service logs and ignores a failure here (a loop closed at shutdown).
            loop.call_soon_threadsafe(queue.put_nowait, ("stage", {"stage": stage, **details}))

        async def run() -> None:
            try:
                answer = await run_in_threadpool(call, body, principal, tell)
                await queue.put(("answer", _answer_out(answer).model_dump(mode="json")))
            except Exception as error:
                failure = _http_error(error)
                await queue.put(
                    ("error", {"status": failure.status_code, "detail": failure.detail})
                )
            finally:
                await run_in_threadpool(held.__exit__, None, None, None)
                await queue.put(None)

        # Held here until done: the stream may be dropped by a client that leaves, and a
        # task only weakly referenced could be collected with its answer half stored.
        task = asyncio.create_task(run())
        _running.add(task)
        task.add_done_callback(_running.discard)

        async def events() -> AsyncIterator[str]:
            while (item := await queue.get()) is not None:
                name, data = item
                yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
            await task

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/conversations", response_model=list[ConversationSummaryOut])
    def list_conversations(principal: Reader) -> list[ConversationSummaryOut]:
        return [
            ConversationSummaryOut(**vars(c))
            for c in chat.conversations(principal.tenant_id, principal.actor)
        ]

    @router.get("/conversations/{conversation_id}", response_model=ConversationOut)
    def read_conversation(conversation_id: uuid.UUID, principal: Reader) -> ConversationOut:
        try:
            messages = chat.conversation(principal.tenant_id, principal.actor, conversation_id)
        except ConversationNotFound as error:
            raise HTTPException(404, "Not found.") from error
        return ConversationOut(
            id=conversation_id,
            messages=[
                MessageOut(
                    id=m.id,
                    question=m.question,
                    answer=m.answer,
                    status=m.status,
                    citations=[CitationOut(**c) for c in m.citations],
                    created_at=m.created_at,
                )
                for m in messages
            ],
        )

    @router.delete("/conversations/{conversation_id}", status_code=204)
    def delete_conversation(conversation_id: uuid.UUID, principal: Reader) -> Response:
        """The conversation and every question and answer in it, deleted for good."""
        try:
            chat.delete(principal.tenant_id, principal.actor, conversation_id)
        except ConversationNotFound as error:
            raise HTTPException(404, "Not found.") from error
        return Response(status_code=204)

    return router
