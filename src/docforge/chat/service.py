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

import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from docforge.chat.injection import reads_as_instructions
from docforge.chat.prompt import CHAT_PROMPT_VERSION, SYSTEM_INSTRUCTION, Passage, build_prompt
from docforge.chat.statements import CODE, RawStatement, check_statements
from docforge.chat.verify import cited_blocks
from docforge.collections import CollectionNotFound, CollectionService
from docforge.db.models import Conversation, Document, Message
from docforge.db.session import SessionFactory
from docforge.db.tenancy import scoped
from docforge.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse
from docforge.llm.replay import NotRecorded
from docforge.search.service import Mode, SearchHit, SearchHits
from docforge.telemetry import traced

logger = logging.getLogger(__name__)
Status = Literal["supported", "partly_supported", "unsupported", "not_found"]

NOT_FOUND = "The documents do not say."
NOT_RECORDED = (
    "This demo answers only the questions it has recorded, and this one was not recorded. "
    "Ask one of its recorded questions; a deployment with a model key answers any question."
)
WITHHELD = (
    "An answer was drafted, but none of its quotes could be found in the documents, so it "
    "is not shown. Try asking about a specific document or value."
)
DEFAULT_PASSAGES = 8
DEFAULT_DAILY_LIMIT = 500
_HISTORY = 3  # earlier turns given with a follow-up
_ANSWERED = ("supported", "partly_supported")
_ANCHORS = 3  # documents an earlier answer cited, searched first for a follow-up
_ANCHORED = 4  # passages from each
_NAMED = 2  # documents a question names by code, searched within
_MIN_CODE = 5  # characters: "Q3" or "A4" names no one document
_BOX_KEYS = ("page", "x0", "y0", "x1", "y1", "page_width", "page_height")


class QuestionLimitReached(Exception):
    """The organisation has asked its questions for today."""


class ConversationNotFound(LookupError):
    """No conversation with that id belongs to this person in this organisation."""


class ScopeConflict(ValueError):
    """A follow-up named a different document from the one its conversation is about."""


class ScopeGone(LookupError):
    """The document or knowledge base a conversation was about has been deleted."""


@dataclass(frozen=True)
class Scope:
    document_id: uuid.UUID | None = None
    collection_id: uuid.UUID | None = None

    @property
    def kind(self) -> str:
        if self.document_id is not None:
            return "document"
        if self.collection_id is not None:
            return "collection"
        return "organisation"


Progress = Callable[[str, dict[str, Any]], None]


class DocumentNotFound(LookupError):
    """The document a question is about does not exist in this organisation."""


class AnswerInvalid(LLMError):
    """The model's reply did not fit the answer's shape, twice."""

    def __init__(self, message: str, responses: tuple[LLMResponse, ...]) -> None:
        super().__init__(message)
        self.responses = responses  # the calls made, so what they cost is recorded


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
        collection_id: uuid.UUID | None = ...,
    ) -> SearchHits: ...


class RawAnswer(BaseModel):  # no extra="forbid": Gemini's schema has no additionalProperties
    statements: list[RawStatement] = Field(
        description="The answer, one claim per statement, each with its citations."
    )
    unanswerable: bool
    missing: str = Field(
        default="", description="If unanswerable: what the passages lack, in a few words."
    )


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
    dropped_statements: int  # statements without a found quote, or with a figure it lacks
    # Why there is no answer: no_passages, not_in_passages, quotes_not_found,
    # figures_not_in_quotes (None when there is one), and what is known of it.
    reason: str | None
    reason_detail: dict[str, Any]
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
class UnansweredQuestion:
    message_id: uuid.UUID
    question: str
    reason: str
    missing: str  # what the passages lacked, as the model said
    documents: list[str]  # what was read for it
    owner: str
    conversation_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True)
class Unanswered:
    questions: list[UnansweredQuestion]
    by_reason: dict[str, int]  # over the last 30 days


@dataclass(frozen=True)
class ConversationSummary:
    id: uuid.UUID
    title: str
    document_id: uuid.UUID | None
    created_at: datetime


def _checked(
    raw: RawAnswer, passages: Sequence[Passage], hits: Sequence[SearchHit], given: str
) -> tuple[Status, str, tuple[Citation, ...], int, int, str | None, dict[str, Any]]:
    """Status, text, citations, the citations and statements dropped, and why there is no
    answer (with what is known of it), if there is none."""
    if raw.unanswerable:
        detail: dict[str, Any] = {
            "missing": raw.missing.strip()[:300],
            "documents": list(dict.fromkeys(hit.filename for hit in hits))[:8],
        }
        return "not_found", NOT_FOUND, (), 0, 0, "not_in_passages", detail
    checked = check_statements(raw.statements, passages, given=given)
    citations: list[Citation] = []
    for kept in checked.kept:
        for index, quote in kept.citations:
            hit = hits[index]
            blocks = cited_blocks(quote, hit.blocks)
            citation = Citation(
                document_id=hit.document_id,
                filename=hit.filename,
                doc_type=hit.doc_type,
                page=int(blocks[0]["page"]) if blocks else hit.page,
                quote=quote,
                # Where the quote is; failing that, where the passage is.
                boxes=tuple({k: block[k] for k in _BOX_KEYS if k in block} for block in blocks)
                or hit.boxes,
            )
            if citation not in citations:
                citations.append(citation)
    if not checked.kept:
        reason = (
            "figures_not_in_quotes"
            if checked.dropped_for_figures
            else "wording_not_in_passages"
            if checked.dropped_for_wording
            else "quotes_not_found"
        )
        detail = {"statements": checked.dropped_statements, "quotes": checked.dropped_citations}
        return (
            "unsupported",
            WITHHELD,
            (),
            checked.dropped_citations,
            checked.dropped_statements,
            reason,
            detail,
        )
    dropped = checked.dropped_citations + checked.dropped_statements
    status: Status = "partly_supported" if dropped else "supported"
    text = " ".join(kept.text for kept in checked.kept)
    return (
        status,
        text,
        tuple(citations),
        checked.dropped_citations,
        checked.dropped_statements,
        None,
        {},
    )


class ChatService:
    def __init__(
        self,
        sessions: SessionFactory,
        search: Search,
        provider: LLMProvider,
        *,
        passages: int = DEFAULT_PASSAGES,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        daily_limit_per_person: int | None = None,
    ) -> None:
        self._sessions = sessions
        self._search = search
        self._provider = provider
        self._passages = passages
        self._daily_limit = daily_limit
        self._per_person = daily_limit_per_person if daily_limit_per_person else daily_limit

    @scoped
    def ask(
        self,
        tenant_id: uuid.UUID,
        owner: str,
        question: str,
        *,
        document_id: uuid.UUID | None = None,
        collection_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
        progress: Progress | None = None,
    ) -> Answer:
        """Raises `QuestionLimitReached`, `ConversationNotFound`, `DocumentNotFound`,
        `CollectionNotFound`, `ScopeConflict`, `ScopeGone`, or `LLMError` when the model
        cannot answer. `progress` is told each stage: searching, reading, checking.

        The question takes its place for the day before the model is asked, so the limit
        holds when questions arrive together and counts those that fail. It is completed
        with the answer, or marked an error with what it cost.
        """
        question = question.strip()
        if document_id is not None and collection_id is not None:
            raise ScopeConflict("a question is about one document or one knowledge base")
        tell = _safe(progress)
        conversation_id, message_id, scope, history, anchors = self._reserve(
            tenant_id, owner, question, Scope(document_id, collection_id), conversation_id
        )
        responses: tuple[LLMResponse, ...] = ()
        try:
            with traced("chat.answer") as span:
                tell("searching", {})
                hits = self._retrieve(
                    tenant_id, _follow_up_query(question, history), scope, anchors, question
                )
                hits, held_back = self._hold_back(tenant_id, question, scope, hits)
                span.set_attribute("docforge.chat.passages", len(hits))
                span.set_attribute("docforge.chat.held_back", held_back)
                span.set_attribute("docforge.chat.scope", scope.kind)
                passages = [
                    Passage(n=n, filename=hit.filename, page=hit.page, text=hit.text, kind=hit.kind)
                    for n, hit in enumerate(hits, start=1)
                ]
                tell("reading", {"passages": len(passages)})
                citations: tuple[Citation, ...] = ()
                reason: str | None
                detail: dict[str, Any]
                if not passages:
                    status: Status = "not_found"
                    text, dropped, dropped_statements = NOT_FOUND, 0, 0
                    reason, detail = "no_passages", {"scope": scope.kind}
                else:
                    try:
                        raw, responses = self._ask(build_prompt(passages, question, history))
                    except NotRecorded:
                        # A replaying deployment (the demo): trying again would not help.
                        raw = None
                    tell("checking", {})
                    if raw is None:
                        status, text, dropped, dropped_statements = "not_found", NOT_RECORDED, 0, 0
                        reason, detail = "not_recorded", {}
                    else:
                        # Figures a statement repeats from the question or the conversation
                        # need no quote of their own.
                        given = " ".join([question, *(f"{q} {a}" for q, a in history)])
                        (status, text, citations, dropped, dropped_statements, reason, detail) = (
                            _checked(raw, passages, hits, given)
                        )
                if held_back:
                    detail = {**detail, "held_back": held_back}
                span.set_attribute("docforge.chat.status", status)
                span.set_attribute("docforge.chat.dropped_citations", dropped)
                span.set_attribute("docforge.chat.dropped_statements", dropped_statements)
        except Exception as error:
            spent = getattr(error, "responses", responses)
            self._complete(
                message_id, "error", "The model could not answer.", (), 0, spent,
                reason="model_error", detail={"error": type(error).__name__},
            )  # fmt: skip
            raise
        if not passages:
            tell("checking", {})  # nothing to check, but every stream shows the same stages
        self._complete(
            message_id, status, text, citations, dropped, responses, dropped_statements,
            reason=reason, detail=detail,
        )  # fmt: skip
        input_tokens, output_tokens = _tokens(responses)
        return Answer(
            conversation_id=conversation_id,
            message_id=message_id,
            status=status,
            text=text,
            citations=citations,
            dropped_citations=dropped,
            dropped_statements=dropped_statements,
            reason=reason,
            reason_detail=detail,
            words_only=hits.words_only,
            model=responses[-1].model if responses else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _retrieve(
        self,
        tenant_id: uuid.UUID,
        query: str,
        scope: "Scope",
        anchors: Sequence[uuid.UUID],
        question: str | None = None,
    ) -> SearchHits:
        """The passages to answer from. A follow-up ("what is its total?") names nothing, so
        the documents its conversation has cited are searched first, each on its own, and
        their passages come before the rest: what "it" is stays in front of the model, however
        many other documents match the words."""
        found: list[SearchHit] = []
        words_only = False
        for anchor in anchors:
            if scope.document_id is not None and anchor != scope.document_id:
                continue  # never outside the conversation's own scope
            near = self._search.search(
                tenant_id,
                query,
                k=_ANCHORED,
                mode="hybrid",
                document_id=anchor,
                collection_id=scope.collection_id,
            )
            found += near
            words_only = words_only or near.words_only
        # The cited documents carry the conversation's subject; the search as a whole then
        # looks for the question itself, so the codes of earlier answers (which keyword search
        # requires a passage to print) do not hold it to those documents.
        wide = self._search.search(
            tenant_id,
            question if anchors and question else query,
            k=self._passages,
            mode="hybrid",
            document_id=scope.document_id,
            collection_id=scope.collection_id,
        )
        # A question that names a document's code (an invoice number) is about that document,
        # but only its header prints the code, not its line rows: search within it too. Only
        # codes long enough to name one document, matched whole, and at most two documents.
        codes = [
            re.compile(rf"(?<!\w){re.escape(code)}(?!\w)", re.IGNORECASE)
            for code in dict.fromkeys(CODE.findall(query))
            if len(code) >= _MIN_CODE
        ]
        named = [hit.document_id for hit in wide if any(code.search(hit.text) for code in codes)]
        for document_id in list(dict.fromkeys(named))[:_NAMED]:
            if document_id in anchors:
                continue
            near = self._search.search(
                tenant_id,
                query,
                k=_ANCHORED,
                mode="hybrid",
                document_id=document_id,
                collection_id=scope.collection_id,
            )
            found += near
            words_only = words_only or near.words_only
        # The documents in question first, but at most half the places: the rest go to the
        # search as a whole, so a follow-up about another document can still reach it.
        seen: set[tuple[uuid.UUID, int, str]] = set()
        merged: list[SearchHit] = []

        def add(hits: Sequence[SearchHit], limit: int) -> None:
            for hit in hits:
                key = (hit.document_id, hit.page, hit.text)
                if len(merged) < limit and key not in seen:
                    seen.add(key)
                    merged.append(hit)

        add(found, max(1, self._passages // 2) if found else 0)
        add(wide, self._passages)
        add(found, self._passages)  # any places left
        return SearchHits(merged, words_only=words_only or wide.words_only)

    def _hold_back(
        self, tenant_id: uuid.UUID, question: str, scope: "Scope", hits: SearchHits
    ) -> tuple[SearchHits, int]:
        """A passage that reads like instructions to the model is held back: a document is
        data, and one that tries to direct the answer is not read as evidence. Its place goes
        to the next passage the search finds, so a planted passage cannot crowd out the rest.
        Held-back passages are logged, for an administrator to look at."""
        held = [hit for hit in hits if reads_as_instructions(hit.text)]
        if not held:
            return hits, 0
        logger.warning(
            "held back %d passage(s) that read like instructions: %s",
            len(held),
            [f"{hit.document_id}:p{hit.page}" for hit in held],
        )
        kept = [hit for hit in hits if not reads_as_instructions(hit.text)]
        seen = {(hit.document_id, hit.page, hit.text) for hit in hits}
        more = self._search.search(
            tenant_id,
            question,
            k=self._passages + len(held),
            mode="hybrid",
            document_id=scope.document_id,
            collection_id=scope.collection_id,
        )
        for hit in more:
            key = (hit.document_id, hit.page, hit.text)
            fresh = key not in seen and not reads_as_instructions(hit.text)
            if len(kept) < self._passages and fresh:
                seen.add(key)
                kept.append(hit)
        return SearchHits(kept, words_only=hits.words_only or more.words_only), len(held)

    def _reserve(
        self,
        tenant_id: uuid.UUID,
        owner: str,
        question: str,
        asked: "Scope",
        conversation_id: uuid.UUID | None,
    ) -> tuple[uuid.UUID, uuid.UUID, "Scope", list[tuple[str, str]], list[uuid.UUID]]:
        """Check the limits and take the question's place, in one transaction under a lock
        per organisation, so two questions cannot both take the last place."""
        with self._sessions.begin() as session:
            lock = func.hashtext(f"chat:{tenant_id}")
            session.execute(select(func.pg_advisory_xact_lock(lock)))
            today = Message.created_at >= func.date_trunc("day", func.now(), "UTC")
            count = session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.tenant_id == tenant_id, today)
            )
            if (count or 0) >= self._daily_limit:
                raise QuestionLimitReached(f"{self._daily_limit} questions a day")
            mine = session.scalar(
                select(func.count())
                .select_from(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(Message.tenant_id == tenant_id, Conversation.owner == owner, today)
            )
            if (mine or 0) >= self._per_person:
                raise QuestionLimitReached(f"{self._per_person} questions a day per person")

            history: list[tuple[str, str]] = []
            anchors: list[uuid.UUID] = []
            if conversation_id is not None:
                conversation = self._owned(session, tenant_id, owner, conversation_id)
                scope = Scope(conversation.document_id, conversation.collection_id)
                # Its document or knowledge base was deleted: continuing would widen the
                # question to the whole organisation, so it is refused instead. Checked
                # first: resending the deleted id is not a conflict, it is gone.
                if conversation.scope != scope.kind:
                    raise ScopeGone(conversation.scope)
                if asked.kind != "organisation" and asked != scope:
                    raise ScopeConflict("a conversation keeps the scope it began with")
                earlier = session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id, Message.status != "pending")
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(_HISTORY)
                ).all()
                # What was asked, and what was answered: a non-answer is not an answer to
                # build on.
                history = [
                    (m.question, m.answer if m.status in _ANSWERED else "(no answer found)")
                    for m in reversed(earlier)
                ]
                # The documents the conversation is about: those its answers cited, newest
                # first.
                anchors = list(
                    dict.fromkeys(
                        uuid.UUID(c["document_id"])
                        for m in earlier
                        if m.status in _ANSWERED
                        for c in m.citations
                    )
                )[:_ANCHORS]
            else:
                scope = asked
                if scope.document_id is not None:
                    found = session.scalar(
                        select(Document.id).where(
                            Document.tenant_id == tenant_id, Document.id == scope.document_id
                        )
                    )
                    if found is None:
                        raise DocumentNotFound(scope.document_id)
                if scope.collection_id is not None and not CollectionService.exists(
                    session, tenant_id, scope.collection_id
                ):
                    raise CollectionNotFound(scope.collection_id)
                conversation = Conversation(
                    tenant_id=tenant_id,
                    owner=owner,
                    document_id=scope.document_id,
                    collection_id=scope.collection_id,
                    scope=scope.kind,
                    title=question[:200],
                )
                session.add(conversation)
                session.flush()
            message = Message(
                tenant_id=tenant_id,
                conversation_id=conversation.id,
                question=question,
                answer="",
                status="pending",
                citations=[],
                prompt_version=CHAT_PROMPT_VERSION,
            )
            session.add(message)
            session.flush()
            return conversation.id, message.id, scope, history, anchors

    def _complete(
        self,
        message_id: uuid.UUID,
        status: str,
        text: str,
        citations: tuple[Citation, ...],
        dropped: int,
        responses: tuple[LLMResponse, ...],
        dropped_statements: int = 0,
        *,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        input_tokens, output_tokens = _tokens(responses)
        try:
            with self._sessions.begin() as session:
                message = session.get_one(Message, message_id)
                message.status = status
                message.answer = text
                message.citations = [c.as_json() for c in citations]
                message.dropped_citations = dropped
                message.dropped_statements = dropped_statements
                message.reason = reason
                message.reason_detail = detail or {}
                message.provider = responses[-1].provider if responses else None
                message.model = responses[-1].model if responses else None
                message.input_tokens = input_tokens
                message.output_tokens = output_tokens
        except Exception:
            # The answer was paid for: say what it cost even if it could not be stored.
            logger.exception(
                "could not store message %s (%s; %s tokens in, %s out)",
                message_id, status, input_tokens, output_tokens,
            )  # fmt: skip
            raise

    @scoped
    def delete(self, tenant_id: uuid.UUID, owner: str, conversation_id: uuid.UUID) -> None:
        """A person's conversation, and every question and answer in it, deleted."""
        with self._sessions.begin() as session:
            conversation = self._owned(session, tenant_id, owner, conversation_id)
            session.execute(delete(Message).where(Message.conversation_id == conversation.id))
            session.delete(conversation)

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
            raise AnswerInvalid(
                "the model's reply did not fit the answer's shape", (first, second)
            ) from error

    @scoped
    def conversation(
        self, tenant_id: uuid.UUID, owner: str, conversation_id: uuid.UUID
    ) -> list[StoredMessage]:
        with self._sessions() as session:
            self._owned(session, tenant_id, owner, conversation_id)
            rows = session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id, Message.status != "pending")
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
    def unanswered(self, tenant_id: uuid.UUID, *, limit: int = 100) -> Unanswered:
        """The organisation's questions that went unanswered, newest first, with why: what
        its documents cannot answer, so what to add. For administrators."""
        with self._sessions() as session:
            rows = session.execute(
                select(Message, Conversation.owner)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(Message.tenant_id == tenant_id, Message.reason.is_not(None))
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
            ).all()
            counts = session.execute(
                select(Message.reason, func.count())
                .where(
                    Message.tenant_id == tenant_id,
                    Message.reason.is_not(None),
                    Message.created_at >= func.now() - text("interval '30 days'"),
                )
                .group_by(Message.reason)
            ).all()
        return Unanswered(
            questions=[
                UnansweredQuestion(
                    message_id=m.id,
                    question=m.question,
                    reason=str(m.reason),
                    missing=str((m.reason_detail or {}).get("missing", "")),
                    documents=list((m.reason_detail or {}).get("documents", [])),
                    owner=owner,
                    conversation_id=m.conversation_id,
                    created_at=m.created_at,
                )
                for m, owner in rows
            ],
            by_reason={str(reason): int(n) for reason, n in counts},
        )

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


def _follow_up_query(question: str, history: Sequence[tuple[str, str]]) -> str:
    """What a follow-up is searched with: every earlier question of the conversation and the
    codes its answers named, then the question. Only the question before was used until
    the hardening checkpoint, and a third turn lost what the first one named."""
    if not history:
        return question
    codes = [code for _, answer in history for code in CODE.findall(answer)]
    return " ".join([*(asked for asked, _ in history), *dict.fromkeys(codes), question])


def _safe(progress: Progress | None) -> Progress:
    """A listener whose failure (a closed stream, a stopped server) is logged, never the
    question's: the answer is still checked, stored and counted."""

    def tell(stage: str, details: dict[str, Any]) -> None:
        if progress is None:
            return
        try:
            progress(stage, details)
        except Exception:
            logger.warning("progress listener failed at stage %s", stage, exc_info=True)

    return tell


def _tokens(responses: Sequence[LLMResponse]) -> tuple[int | None, int | None]:
    """Input and output tokens of every call; thinking is billed, and counted, as output.
    None when no call reported them."""
    if not responses or all(r.input_tokens is None for r in responses):
        return None, None
    return (
        sum(r.input_tokens or 0 for r in responses),
        sum((r.output_tokens or 0) + (r.thinking_tokens or 0) for r in responses),
    )
