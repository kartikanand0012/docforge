"""Chat with documents.

A question is answered from the passages hybrid search finds (in one document, or across the
organisation), by a model with no tools that must cite an exact quote for what it says. The
answer is then checked as extracted values are:

- `supported`: every citation's quote is in the passage it names.
- `partly_supported`: some are; the others are dropped and counted.
- `unsupported`: none is. The answer is withheld, because nothing shows it is true.
- `not_found`: the model said the passages do not answer it, or there was nothing to read.

A conversation belongs to the person who started it. Questions and answers are stored in
`messages`, never in the audit log or a trace: a question may name a patient or a price.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docforge.chat.prompt import CHAT_PROMPT_VERSION, SYSTEM_INSTRUCTION, Passage, build_prompt
from docforge.chat.verify import cited_blocks, quote_in
from docforge.db.models import Conversation, Document, Message
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse
from docforge.search.service import Mode, SearchHit, SearchHits
from docforge.telemetry import traced

Status = Literal["supported", "partly_supported", "unsupported", "not_found"]

NOT_FOUND = "The documents do not say."
WITHHELD = (
    "An answer was drafted, but none of its quotes could be found in the documents, so it "
    "is not shown. Try asking about a specific document or value."
)
DEFAULT_PASSAGES = 8
DEFAULT_DAILY_LIMIT = 500
_HISTORY = 3  # earlier turns given with a follow-up
_BOX_KEYS = ("page", "x0", "y0", "x1", "y1", "page_width", "page_height")


class QuestionLimitReached(Exception):
    """The organisation has asked its questions for today."""


class ConversationNotFound(LookupError):
    """No conversation with that id belongs to this person in this organisation."""


class DocumentNotFound(LookupError):
    """The document a question is about does not exist in this organisation."""


class AnswerInvalid(LLMError):
    """The model's reply did not fit the answer's shape, twice."""


class Search(Protocol):
    def search(
        self,
        tenant_id: uuid.UUID,
        query: str,
        *,
        k: int = ...,
        mode: Mode = ...,
        doc_type: str | None = ...,
        document_id: uuid.UUID | None = ...,
    ) -> SearchHits: ...


class RawCitation(BaseModel):  # no extra="forbid": Gemini's schema has no additionalProperties
    passage: int = Field(description="The number of the passage quoted.")
    quote: str = Field(description="Words copied exactly from that passage.")


class RawAnswer(BaseModel):
    answer: str
    citations: list[RawCitation]
    unanswerable: bool


@dataclass(frozen=True)
class Citation:
    document_id: uuid.UUID
    filename: str
    doc_type: str
    page: int
    quote: str
    boxes: tuple[dict[str, Any], ...]  # where the quote is on the page

    def as_json(self) -> dict[str, Any]:
        return {
            "document_id": str(self.document_id),
            "filename": self.filename,
            "doc_type": self.doc_type,
            "page": self.page,
            "quote": self.quote,
            "boxes": list(self.boxes),
        }


@dataclass(frozen=True)
class Answer:
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    status: Status
    text: str
    citations: tuple[Citation, ...]
    dropped_citations: int  # quotes not found in the passages they named
    words_only: bool  # the passages were found by words alone (no embedding service)
    model: str | None
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class StoredMessage:
    id: uuid.UUID
    question: str
    answer: str
    status: str
    citations: list[dict[str, Any]]
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    created_at: datetime


@dataclass(frozen=True)
class ConversationSummary:
    id: uuid.UUID
    title: str
    document_id: uuid.UUID | None
    created_at: datetime


def _checked(
    raw: RawAnswer, passages: Sequence[Passage], hits: Sequence[SearchHit]
) -> tuple[Status, str, tuple[Citation, ...], int]:
    if raw.unanswerable:
        return "not_found", NOT_FOUND, (), 0
    citations: list[Citation] = []
    dropped = 0
    for cited in raw.citations:
        index = cited.passage - 1
        if not 0 <= index < len(passages) or not quote_in(cited.quote, passages[index].text):
            dropped += 1
            continue
        hit = hits[index]
        blocks = cited_blocks(cited.quote, hit.blocks)
        citation = Citation(
            document_id=hit.document_id,
            filename=hit.filename,
            doc_type=hit.doc_type,
            page=int(blocks[0]["page"]) if blocks else hit.page,
            quote=cited.quote,
            # Where the quote is; failing that, where the passage is.
            boxes=tuple(
                {k: block[k] for k in _BOX_KEYS if k in block}
                for block in blocks
            )
            or hit.boxes,
        )
        if citation not in citations:
            citations.append(citation)
    if not citations:
        return "unsupported", WITHHELD, (), dropped
    status: Status = "partly_supported" if dropped else "supported"
    return status, raw.answer.strip(), tuple(citations), dropped


class ChatService:
    def __init__(
        self,
        sessions: SessionFactory,
        search: Search,
        provider: LLMProvider,
        *,
        passages: int = DEFAULT_PASSAGES,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
    ) -> None:
        self._sessions = sessions
        self._search = search
        self._provider = provider
        self._passages = passages
        self._daily_limit = daily_limit

    @scoped
    def ask(
        self,
        tenant_id: uuid.UUID,
        owner: str,
        question: str,
        *,
        document_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> Answer:
        """Raises `QuestionLimitReached`, `ConversationNotFound`, `DocumentNotFound`, or
        `LLMError` when the model cannot answer."""
        question = question.strip()
        with self._sessions() as session:
            asked_today = session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.tenant_id == tenant_id, Message.created_at >= _midnight())
            )
            if (asked_today or 0) >= self._daily_limit:
                raise QuestionLimitReached(f"{self._daily_limit} questions a day")
            history: list[tuple[str, str]] = []
            if conversation_id is not None:
                conversation = self._owned(session, tenant_id, owner, conversation_id)
                document_id = conversation.document_id
                earlier = session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(_HISTORY)
                ).all()
                history = [(m.question, m.answer) for m in reversed(earlier)]
            elif document_id is not None:
                exists = session.scalar(
                    select(Document.id).where(
                        Document.tenant_id == tenant_id, Document.id == document_id
                    )
                )
                if exists is None:
                    raise DocumentNotFound(document_id)

        with traced("chat.answer") as span:
            # A follow-up such as "and its batch?" is searched with the question before it.
            query = f"{history[-1][0]} {question}" if history else question
            hits = self._search.search(
                tenant_id, query, k=self._passages, mode="hybrid", document_id=document_id
            )
            span.set_attribute("docforge.chat.passages", len(hits))
            span.set_attribute("docforge.chat.scoped", document_id is not None)
            passages = [
                Passage(n=n, filename=hit.filename, page=hit.page, text=hit.text)
                for n, hit in enumerate(hits, start=1)
            ]
            responses: tuple[LLMResponse, ...] = ()
            citations: tuple[Citation, ...] = ()
            if not passages:
                status: Status = "not_found"
                text, dropped = NOT_FOUND, 0
            else:
                raw, responses = self._ask(build_prompt(passages, question, history))
                status, text, citations, dropped = _checked(raw, passages, hits)
            span.set_attribute("docforge.chat.status", status)
            span.set_attribute("docforge.chat.dropped_citations", dropped)

        input_tokens = sum(r.input_tokens or 0 for r in responses) if responses else None
        output_tokens = (
            sum((r.output_tokens or 0) + (r.thinking_tokens or 0) for r in responses)
            if responses
            else None
        )
        model = responses[-1].model if responses else None
        with self._sessions.begin() as session:
            if conversation_id is None:
                conversation = Conversation(
                    tenant_id=tenant_id,
                    owner=owner,
                    document_id=document_id,
                    title=question[:200],
                )
                session.add(conversation)
                session.flush()
                conversation_id = conversation.id
            message = Message(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                question=question,
                answer=text,
                status=status,
                citations=[c.as_json() for c in citations],
                dropped_citations=dropped,
                model=model,
                prompt_version=CHAT_PROMPT_VERSION,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            session.add(message)
            session.flush()
            message_id = message.id
        return Answer(
            conversation_id=conversation_id,
            message_id=message_id,
            status=status,
            text=text,
            citations=citations,
            dropped_citations=dropped,
            words_only=hits.words_only,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _ask(self, prompt: str) -> tuple[RawAnswer, tuple[LLMResponse, ...]]:
        """One request, asked again once with what was wrong if the reply does not fit."""
        request = LLMRequest(
            system=SYSTEM_INSTRUCTION,
            prompt=prompt,
            schema=RawAnswer,
            prompt_version=CHAT_PROMPT_VERSION,
        )
        first = self._provider.generate(request)
        try:
            return RawAnswer.model_validate_json(first.text), (first,)
        except ValidationError:
            retry = replace(
                request,
                prompt=f"{prompt}\nYour previous reply was not valid JSON of the required "
                "shape. Reply again with JSON that fits the schema.\n",
            )
        second = self._provider.generate(retry)
        try:
            return RawAnswer.model_validate_json(second.text), (first, second)
        except ValidationError as error:
            raise AnswerInvalid("the model's reply did not fit the answer's shape") from error

    @scoped
    def conversation(
        self, tenant_id: uuid.UUID, owner: str, conversation_id: uuid.UUID
    ) -> list[StoredMessage]:
        with self._sessions() as session:
            self._owned(session, tenant_id, owner, conversation_id)
            rows = session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at, Message.id)
            ).all()
            return [
                StoredMessage(
                    id=m.id,
                    question=m.question,
                    answer=m.answer,
                    status=m.status,
                    citations=list(m.citations),
                    model=m.model,
                    input_tokens=m.input_tokens,
                    output_tokens=m.output_tokens,
                    created_at=m.created_at,
                )
                for m in rows
            ]

    @scoped
    def conversations(
        self, tenant_id: uuid.UUID, owner: str, *, limit: int = 50
    ) -> list[ConversationSummary]:
        with self._sessions() as session:
            found = session.scalars(
                select(Conversation)
                .where(Conversation.tenant_id == tenant_id, Conversation.owner == owner)
                .order_by(Conversation.created_at.desc(), Conversation.id.desc())
                .limit(limit)
            ).all()
            return [
                ConversationSummary(
                    id=c.id, title=c.title, document_id=c.document_id, created_at=c.created_at
                )
                for c in found
            ]

    @staticmethod
    def _owned(
        session: Session, tenant_id: uuid.UUID, owner: str, conversation_id: uuid.UUID
    ) -> Conversation:
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.tenant_id == tenant_id,
                Conversation.owner == owner,
                Conversation.id == conversation_id,
            )
        )
        if conversation is None:
            raise ConversationNotFound(conversation_id)
        return conversation


def _midnight() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
