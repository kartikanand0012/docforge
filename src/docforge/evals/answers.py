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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from docforge.chat.prompt import CHAT_PROMPT_VERSION
from docforge.chat.service import ChatService
from docforge.chat.verify import normalise
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


class AnswerResult(_Model):
    question_id: str
    status: str
    text: str
    cited_documents: tuple[str, ...]
    cross_tenant: int  # citations of another organisation's documents
    input_tokens: int | None
    output_tokens: int | None


class AnswerReport(_Model):
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
    cross_tenant_citations: int
    input_tokens_per_question: int  # mean, over questions the model was asked
    output_tokens_per_question: int
    missed: tuple[str, ...]
    answers: dict[str, str] = {}  # question id -> what was said, for reading the misses


def _label(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def build_questions(synthetic: Path, coa: Path, pairs: int = 8) -> list[Question]:
    questions: list[Question] = []
    for index in range(1, pairs + 1):
        pair, case = f"pair_{index:03d}", f"coa_{index:03d}"
        tenant = _tenant(index, pairs)
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
        ) -> None:
            questions.append(
                Question(
                    id=f"{pair}-{qid}",
                    text=text,
                    tenant=tenant,
                    expect=expect,
                    documents=documents,
                )
            )

        ask(
            "total",
            f"What is the grand total of invoice {number}?",
            Expect(kind="number", value=invoice["totals"]["grand_total"]),
            here,
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
        ask("bank", f"What is the bank account number of {seller}?", None, ())
        ask("phone", f"What is the phone number of {invoice['buyer']['name']}?", None, ())
        ask("microbial", f"What was the total microbial count for batch {batch}?", None, ())
    return questions


def _numbers(text: str) -> list[Decimal]:
    found = []
    for token in _NUMBER.findall(text):
        try:
            found.append(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    return found


def _squash(value: str) -> str:
    return re.sub(r"\s+", "", normalise(value))


def correct(text: str, expect: Expect) -> bool:
    if expect.kind == "number":
        return Decimal(expect.value) in _numbers(text)
    return _squash(expect.value) in _squash(text)


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
    answerable = [q for q in questions if q.expect is not None]
    unanswerable = [q for q in questions if q.expect is None]
    missed: list[str] = []
    right = cited = abstained = wrong = 0
    for q in answerable:
        r = by_id[q.id]
        assert q.expect is not None  # noqa: S101 - filtered above
        if r.status in ANSWERED:
            is_right = correct(r.text, q.expect)
            right += is_right
            wrong += not is_right
            cited += any(d in q.documents for d in r.cited_documents)
            if not is_right:
                missed.append(q.id)
        else:
            abstained += 1
            missed.append(q.id)
    held_back = 0
    for q in unanswerable:
        if by_id[q.id].status in ABSTAINED:
            held_back += 1
        else:
            missed.append(q.id)
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
        cross_tenant_citations=sum(r.cross_tenant for r in results),
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
    on_answer: Callable[[Question, AnswerResult], None] | None = None,
) -> AnswerReport:
    questions = build_questions(synthetic, coa, pairs)
    results: list[AnswerResult] = []
    with indexed_corpus(synthetic, coa, recordings, embedder, pairs=pairs) as corpus:
        # Big enough for every question; the limit itself is tested elsewhere.
        chat = ChatService(corpus.sessions, corpus.search, provider, daily_limit=10_000)
        for q in questions:
            answer = chat.ask(corpus.tenants[q.tenant], "eval", q.text)
            found = [corpus.keys.get(uuid.UUID(str(c.document_id))) for c in answer.citations]
            result = AnswerResult(
                question_id=q.id,
                status=answer.status,
                text=answer.text,
                cited_documents=tuple(key for key, _ in filter(None, found)),
                cross_tenant=sum(1 for f in found if f is None or f[1] != q.tenant),
                input_tokens=answer.input_tokens,
                output_tokens=answer.output_tokens,
            )
            results.append(result)
            if on_answer is not None:
                on_answer(q, result)
    return score_answers(
        questions, results, model=provider.model, prompt_version=CHAT_PROMPT_VERSION
    )


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
            f"  cross-tenant citations    {report.cross_tenant_citations}",
            f"  tokens per question       {report.input_tokens_per_question} in, "
            f"{report.output_tokens_per_question} out",
            f"  missed: {', '.join(report.missed) or 'none'}",
        ]
    )
