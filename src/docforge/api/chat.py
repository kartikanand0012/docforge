"""Chat endpoints: ask about the organisation's documents (or one of them), and read back
one's own conversations."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from docforge.api.auth import require
from docforge.api.search import PerCaller
from docforge.auth import Principal
from docforge.chat.service import (
    ChatService,
    ConversationNotFound,
    DocumentNotFound,
    QuestionLimitReached,
)
from docforge.llm.base import LLMError

Reader = Annotated[Principal, Depends(require("documents:read"))]
_UNAVAILABLE = "The model could not answer just now. Try again shortly."


class QuestionIn(BaseModel):
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    document_id: uuid.UUID | None = None
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


def chat_router(chat: ChatService, per_minute: int = 20) -> APIRouter:
    router = APIRouter(prefix="/v1")
    limiter = PerCaller(per_minute)  # each question is a paid model call

    @router.post("/chat", response_model=AnswerOut)
    async def ask(body: QuestionIn, principal: Reader) -> AnswerOut:
        if not limiter.allow(principal.actor):
            raise HTTPException(429, "Too many questions; wait a minute.")
        try:
            answer = await run_in_threadpool(
                chat.ask,
                principal.tenant_id,
                principal.actor,
                body.question,
                document_id=body.document_id,
                conversation_id=body.conversation_id,
            )
        except QuestionLimitReached as error:
            raise HTTPException(
                429, "The organisation's questions for today are used up."
            ) from error
        except (ConversationNotFound, DocumentNotFound) as error:
            raise HTTPException(404, "Not found.") from error
        except LLMError as error:
            raise HTTPException(503, _UNAVAILABLE) from error
        return AnswerOut(
            conversation_id=answer.conversation_id,
            message_id=answer.message_id,
            status=answer.status,
            text=answer.text,
            citations=[CitationOut(**c.as_json()) for c in answer.citations],
            dropped_citations=answer.dropped_citations,
            words_only=answer.words_only,
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

    return router
