"""The answer eval: does chat answer correctly, cite the right document, and say so when the
documents do not hold the answer?

Questions come from the synthetic labels: the grand total and issuer of an invoice, the
quantity of its first line, the assay of a certified batch. Questions the documents cannot
answer (a bank account, a phone number, a test no certificate reports) check abstention.
Everything goes through the real services in a temporary database, split between two
organisations: a citation from the other organisation's documents is counted, and must be 0.
"""

import json
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from docforge.chat.prompt import CHAT_PROMPT_VERSION
from docforge.chat.service import ChatService
from docforge.chat.verify import normalise
from docforge.collections import CollectionService
from docforge.evals.search import _tenant, indexed_corpus
from docforge.llm.base import LLMProvider
from docforge.search.embeddings import Embedder

ANSWERED = ("supported", "partly_supported")
ABSTAINED = ("not_found", "unsupported")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Expect(_Model):
    kind: Literal["number", "text"]
    value: str


class Question(_Model):
    id: str
    text: str
    tenant: str  # a or b
    expect: Expect | None  # None: the documents do not answer it
    documents: tuple[str, ...]  # where the answer is, e.g. pair_001/invoice, coa_001
    scoped: bool = False  # asked about its document alone, not the whole organisation
    collection: str | None = None  # asked within this knowledge base: invoices or certificates
    follows: str | None = None  # a follow-up, asked in the conversation of this question


class AnswerResult(_Model):
    question_id: str
    status: str
    text: str
    cited_documents: tuple[str, ...]
    cross_tenant: int  # citations of another organisation's documents
    outside_collection: int = 0  # citations of documents outside the knowledge base asked
    reason: str | None = None  # why it went unanswered, if it did
    missing: str = ""  # what the passages lacked, as the model said
    input_tokens: int | None
    output_tokens: int | None


class AnswerReport(_Model):
    provider: str = "gemini"  # whose replies were scored
    model: str
    prompt_version: str
    questions: int
    answerable: int
    unanswerable: int
    answered_correctly: float  # answerable questions answered with the expected value
    cited_expected_document: float  # answerable questions answered citing where it is
    false_abstention: float  # answerable questions it said it could not answer
    wrong_answers: int  # answered, cited, and wrong
    abstained_when_no_answer: float
    answered_unanswerable: int  # answered, with checked quotes, what the documents do not say
    follow_ups: int  # questions asked as follow-ups, the second and third of a conversation
    follow_ups_correct: float  # of those, answered with the expected value
    # Unanswerable questions declined with a reason that says what is missing, or that
    # nothing matched: what an organisation reads to know which documents to add.
    explained_when_no_answer: float
    cross_tenant_citations: int
    outside_knowledge_base: int  # citations from outside the knowledge base asked; must be 0
    input_tokens_per_question: int  # mean, over questions the model was asked
    output_tokens_per_question: int
    missed: tuple[str, ...]
    answers: dict[str, str] = {}  # question id -> what was said, for reading the misses


def _label(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def build_questions(
    synthetic: Path, coa: Path, pairs: int = 8, corpus_pairs: int | None = None
) -> list[Question]:
    """Questions about the first `pairs` pairs, in the organisations the corpus of
    `corpus_pairs` puts them in. Conversations are held in the organisation with every
    document ("all"), where the invoice asked about is one of many."""
    questions: list[Question] = []
    for index in range(1, pairs + 1):
        pair, case = f"pair_{index:03d}", f"coa_{index:03d}"
        tenant = _tenant(index, corpus_pairs or pairs)
        invoice = _label(synthetic / pair / "label.json")["invoice"]
        certificate = _label(coa / case / "label.json")["coa"]
        number, seller = invoice["invoice_no"], invoice["seller"]["name"]
        line = invoice["lines"][0]
        assay = next(t for t in certificate["tests"] if t["name"] == "Assay")
        batch = certificate["batch_no"]
        here = (f"{pair}/invoice",)

        def ask(
            qid: str,
            text: str,
            expect: Expect | None,
            documents: tuple[str, ...],
            *,
            pair: str = pair,
            tenant: str = tenant,
            scoped: bool = False,
            collection: str | None = None,
            follows: str | None = None,
            where: str | None = None,
        ) -> None:
            questions.append(
                Question(
                    id=f"{pair}-{qid}",
                    text=text,
                    tenant=where or tenant,
                    expect=expect,
                    documents=documents,
                    scoped=scoped,
                    collection=collection,
                    follows=follows,
                )
            )

        ask(
            "total",
            f"What is the grand total of invoice {number}?",
            Expect(kind="number", value=invoice["totals"]["grand_total"]),
            here,
        )
        ask(
            "total-in-document",
            "What is the grand total of this invoice?",
            Expect(kind="number", value=invoice["totals"]["grand_total"]),
            here,
            scoped=True,
        )
        ask("seller", f"Who issued invoice {number}?", Expect(kind="text", value=seller), here)
        ask(
            "quantity",
            f"How many units of {line['product_name']} were billed on invoice {number}?",
            Expect(kind="number", value=str(line["qty"])),
            here,
        )
        ask(
            "assay",
            f"What was the assay result for batch {batch}?",
            Expect(kind="text", value=assay["result"]),
            (case,),
        )
        # Within a knowledge base: the certificates hold the assay; the invoices, which
        # print the same batch, do not, and must not be answered from elsewhere.
        ask(
            "assay-in-certificates",
            f"What was the assay result for batch {batch}?",
            Expect(kind="text", value=assay["result"]),
            (case,),
            collection="certificates",
        )
        ask(
            "assay-in-invoices",
            f"What was the assay result for batch {batch}?",
            None,
            (),
            collection="invoices",
        )
        # A conversation of three turns: the second and third name nothing but "it", so they
        # can only be answered with what came before.
        ask(
            "chain-1",
            f"Which invoice billed batch {line['batch_no']}?",
            Expect(kind="text", value=number),
            here,
            where="all",
        )
        ask(
            "chain-2",
            "Who issued it?",
            Expect(kind="text", value=seller),
            here,
            follows=f"{pair}-chain-1",
            where="all",
        )
        ask(
            "chain-3",
            "What is its grand total?",
            Expect(kind="number", value=invoice["totals"]["grand_total"]),
            here,
            follows=f"{pair}-chain-2",
            where="all",
        )
        ask("bank", f"What is the bank account number of {seller}?", None, ())
        ask("phone", f"What is the phone number of {invoice['buyer']['name']}?", None, ())
        ask("microbial", f"What was the total microbial count for batch {batch}?", None, ())
    return questions


def _numbers(text: str) -> list[Decimal]:
    """Each number written in `text`, read with Indian or Western digit grouping."""
    found = []
    for token in _NUMBER.findall(text):
        try:
            found.append(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    return found


def correct(text: str, expect: Expect, question: str = "") -> bool:
    """A number answer is right when the expected number is stated and every other number
    in it was in the question (an invoice number, a strength repeated back): a second,
    different figure makes it wrong. A text answer contains the expected text, whole."""
    if expect.kind == "number":
        asked = set(_numbers(question))
        stated = {n for n in _numbers(text) if n not in asked}
        return stated == {Decimal(expect.value)}
    # The expected words in order, spacing optional ("96.3 %" is "96.3%"), not inside a
    # longer word or number ("99" is not in "99.5" or "1999").
    words = normalise(expect.value).split()
    pattern = r"\s*".join(re.escape(word) for word in words)
    return re.search(rf"(?<![\w.]){pattern}(?![\w]|\.\d)", normalise(text)) is not None


def _share(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def score_answers(
    questions: Sequence[Question],
    results: Sequence[AnswerResult],
    *,
    model: str,
    prompt_version: str,
) -> AnswerReport:
    by_id = {r.question_id: r for r in results}
    questions_by_id = {q.id: q for q in questions}

    def said_before(q: Question) -> str:
        """The question and, for a follow-up, every earlier turn: figures named there (an
        invoice number) may be repeated in a right answer."""
        parts, step = [q.text], q.follows
        while step is not None:
            earlier = questions_by_id[step]
            parts += [earlier.text, by_id[step].text]
            step = earlier.follows
        return " ".join(parts)

    answerable = [q for q in questions if q.expect is not None]
    unanswerable = [q for q in questions if q.expect is None]
    missed: list[str] = []
    right = cited = abstained = wrong = 0
    for q in answerable:
        r = by_id[q.id]
        assert q.expect is not None  # noqa: S101 - filtered above
        if r.status in ANSWERED:
            is_right = correct(r.text, q.expect, said_before(q))
            right += is_right
            wrong += not is_right
            cited += any(d in q.documents for d in r.cited_documents)
            if not is_right:
                missed.append(q.id)
        else:
            abstained += 1
            missed.append(q.id)
    held_back = answered_anyway = explained = 0
    for q in unanswerable:
        if by_id[q.id].status in ABSTAINED:
            held_back += 1
            r = by_id[q.id]
            explained += r.reason == "no_passages" or (
                r.reason == "not_in_passages" and bool(r.missing.strip())
            )
        else:
            answered_anyway += 1
            missed.append(q.id)
    follow_ups = [q for q in answerable if q.follows]
    follow_ups_right = sum(
        1
        for q in follow_ups
        if by_id[q.id].status in ANSWERED
        and q.expect
        and correct(by_id[q.id].text, q.expect, said_before(q))
    )
    calls = [r for r in results if r.input_tokens is not None]
    tokens = (
        (
            round(sum(r.input_tokens or 0 for r in calls) / len(calls)),
            round(sum(r.output_tokens or 0 for r in calls) / len(calls)),
        )
        if calls
        else (0, 0)
    )
    return AnswerReport(
        model=model,
        prompt_version=prompt_version,
        questions=len(questions),
        answerable=len(answerable),
        unanswerable=len(unanswerable),
        answered_correctly=_share(right, len(answerable)),
        cited_expected_document=_share(cited, len(answerable)),
        false_abstention=_share(abstained, len(answerable)),
        wrong_answers=wrong,
        abstained_when_no_answer=_share(held_back, len(unanswerable)),
        answered_unanswerable=answered_anyway,
        follow_ups=len(follow_ups),
        follow_ups_correct=_share(follow_ups_right, len(follow_ups)),
        explained_when_no_answer=_share(explained, len(unanswerable)),
        cross_tenant_citations=sum(r.cross_tenant for r in results),
        outside_knowledge_base=sum(r.outside_collection for r in results),
        input_tokens_per_question=tokens[0],
        output_tokens_per_question=tokens[1],
        missed=tuple(missed),
        answers={
            r.question_id: f"[{r.status}] {r.text}" for r in results if r.question_id in missed
        },
    )


def run_answer_eval(
    synthetic: Path,
    coa: Path,
    recordings: Path,
    embedder: Embedder,
    provider: LLMProvider,
    *,
    pairs: int = 8,
    corpus_pairs: int = 20,
    on_answer: Callable[[Question, AnswerResult], None] | None = None,
) -> AnswerReport:
    questions = build_questions(synthetic, coa, pairs, corpus_pairs)
    results: list[AnswerResult] = []
    with indexed_corpus(synthetic, coa, recordings, embedder, pairs=corpus_pairs) as corpus:
        # Big enough for every question; the limit itself is tested elsewhere.
        chat = ChatService(corpus.sessions, corpus.search, provider, daily_limit=10_000)
        ids = {(key, tenant): document_id for document_id, (key, tenant) in corpus.keys.items()}
        bases = _knowledge_bases(corpus)
        conversations: dict[str, uuid.UUID] = {}
        for q in questions:
            scope = ids[(q.documents[0], q.tenant)] if q.scoped else None
            base = bases[(q.tenant, q.collection)] if q.collection else None
            answer = chat.ask(
                corpus.tenants[q.tenant],
                "eval",
                q.text,
                document_id=scope,
                collection_id=base.id if base else None,
                conversation_id=conversations[q.follows] if q.follows else None,
            )
            conversations[q.id] = answer.conversation_id
            found = [corpus.keys.get(uuid.UUID(str(c.document_id))) for c in answer.citations]
            outside = (
                sum(1 for c in answer.citations if c.document_id not in base.members) if base else 0
            )
            result = AnswerResult(
                question_id=q.id,
                status=answer.status,
                text=answer.text,
                cited_documents=tuple(key for key, _ in filter(None, found)),
                cross_tenant=sum(1 for f in found if f is None or f[1] != q.tenant),
                outside_collection=outside,
                reason=answer.reason,
                missing=str(answer.reason_detail.get("missing", "")),
                input_tokens=answer.input_tokens,
                output_tokens=answer.output_tokens,
            )
            results.append(result)
            if on_answer is not None:
                on_answer(q, result)
    return score_answers(
        questions, results, model=provider.model, prompt_version=CHAT_PROMPT_VERSION
    )


@dataclass(frozen=True)
class _Base:
    id: uuid.UUID
    members: frozenset[uuid.UUID]


def _knowledge_bases(corpus: Any) -> dict[tuple[str, str], _Base]:
    """In each organisation: "invoices" (invoices and orders) and "certificates"."""
    collections = CollectionService(corpus.sessions)
    bases: dict[tuple[str, str], _Base] = {}
    for tenant in ("a", "b"):
        tenant_id = corpus.tenants[tenant]
        mine = {d: key for d, (key, t) in corpus.keys.items() if t == tenant}
        for name, wanted in (
            ("invoices", [d for d, key in mine.items() if "/" in key]),
            ("certificates", [d for d, key in mine.items() if key.startswith("coa_")]),
        ):
            created = collections.create(tenant_id, name, actor="eval")
            collections.add(tenant_id, created.id, wanted, actor="eval")
            bases[(tenant, name)] = _Base(created.id, frozenset(wanted))
    return bases


def format_answer_report(report: AnswerReport) -> str:
    return "\n".join(
        [
            f"Answer eval ({report.model}, {report.prompt_version}): "
            f"{report.answerable} answerable, {report.unanswerable} not answerable",
            f"  answered correctly        {report.answered_correctly:.2%}",
            f"  cited the right document  {report.cited_expected_document:.2%}",
            f"  said it could not answer  {report.false_abstention:.2%} (should be low)",
            f"  wrong answers             {report.wrong_answers}",
            f"  abstained when no answer  {report.abstained_when_no_answer:.2%}",
            f"  answered the unanswerable {report.answered_unanswerable}",
            f"  cross-tenant citations    {report.cross_tenant_citations}",
            f"  outside knowledge base    {report.outside_knowledge_base}",
            f"  explained when no answer  {report.explained_when_no_answer:.2%}",
            f"  follow-ups correct        {report.follow_ups_correct:.2%} of {report.follow_ups}",
            f"  tokens per question       {report.input_tokens_per_question} in, "
            f"{report.output_tokens_per_question} out",
            f"  missed: {', '.join(report.missed) or 'none'}",
        ]
    )
