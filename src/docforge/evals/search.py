"""The search eval: does a question find the document it is about?

The synthetic pairs and their certificates are split between two organisations. Every
document goes through the real services (parse and extraction replayed from recordings,
indexing, search) in a temporary database, as the application's role. Each question is asked
in its own organisation (filtered: what a user gets) and against one organisation holding
everything (unfiltered: twice the documents to choose from). A result from the other
organisation in a filtered search is counted; it must be zero.

Recall@5 is the share of a question's expected documents among the first five documents found.
"""

import calendar
import json
import math
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from alembic import command
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from docforge.config import get_settings
from docforge.db import alembic_config
from docforge.db.roles import ensure_app_login
from docforge.db.session import make_engine, make_session_factory
from docforge.documents import DocumentService
from docforge.extraction.coa import COA_SPEC, CoaExtraction
from docforge.extraction.pipeline import ExtractionPipeline, InvoicePipeline
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC, PurchaseOrderExtraction
from docforge.llm.replay import RecordingProvider
from docforge.parsing.cache import CachingParser
from docforge.search.embeddings import Embedder
from docforge.search.service import Mode, SearchService
from docforge.storage import MemoryObjectStore

MODES: tuple[Mode, ...] = ("keyword", "vector", "hybrid")
_EVAL_DATABASE = "docforge_search_eval"
_TOP = 5


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Question(_Model):
    id: str
    text: str
    kind: str  # exact, paraphrase or certificate
    tenant: str  # a or b
    expected: tuple[str, ...]  # document keys, e.g. pair_001/invoice, coa_001


class ModeResult(_Model):
    recall_at_5_filtered: float
    recall_at_5_unfiltered: float
    cross_tenant_hits: int
    by_kind: dict[str, float]
    missed: tuple[str, ...]  # question ids with recall below 1, filtered
    # For each missed question: the first five documents found instead.
    found_instead: dict[str, tuple[str, ...]] = {}


class SearchReport(_Model):
    embedding_model: str
    documents: int
    questions: int
    modes: dict[str, ModeResult]


def _label(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _month(day: str) -> str:
    value = date.fromisoformat(day)
    return f"{calendar.month_name[value.month]} {value.year}"


def _tenant(index: int, pairs: int) -> str:
    return "a" if index <= math.ceil(pairs / 2) else "b"


def build_questions(synthetic: Path, coa: Path, pairs: int = 20) -> list[Question]:
    questions: list[Question] = []
    failing: dict[tuple[str, str], list[str]] = {}
    for index in range(1, pairs + 1):
        pair = f"pair_{index:03d}"
        tenant = _tenant(index, pairs)
        invoice = _label(synthetic / pair / "label.json")["invoice"]
        order = _label(synthetic / pair / "label.json")["purchase_order"]
        line = invoice["lines"][0]
        generic = re.split(r"\s", line["product_name"])[0].lower()
        seller, buyer = invoice["seller"]["name"], invoice["buyer"]["name"]
        inv, po = f"{pair}/invoice", f"{pair}/purchase_order"
        sold_in = _month(invoice["invoice_date"])

        def ask(
            name: str,
            wording: str,
            kind: str,
            *expected: str,
            pair: str = pair,
            tenant: str = tenant,
        ) -> Question:
            return Question(
                id=f"{pair}-{name}", text=wording, kind=kind, tenant=tenant, expected=expected
            )

        questions += [
            ask("batch", f"Which invoice billed batch {line['batch_no']}?", "exact", inv),
            ask("invoice-no", f"invoice number {invoice['invoice_no']}", "exact", inv),
            ask("po-no", f"purchase order {order['po_no']}", "exact", po, inv),
            ask("sold", f"{generic} sold by {seller} to {buyer} in {sold_in}", "paraphrase", inv),
            ask("ordered", f"what {buyer} ordered from {seller}", "paraphrase", po),
        ]
        case = f"coa_{index:03d}"
        coa_label = _label(coa / case / "label.json")
        questions.append(
            Question(
                id=f"{case}-batch",
                text=f"certificate of analysis for batch {coa_label['coa']['batch_no']}",
                kind="certificate",
                tenant=tenant,
                expected=(case,),
            )
        )
        for test in coa_label["out_of_limit"]:
            failing.setdefault((tenant, test), []).append(case)
    wording = {
        "Assay": "Which batches failed the assay test?",
        "Related substances": "Which certificates show related substances above the limit?",
    }
    for (tenant, test), cases in sorted(failing.items()):
        questions.append(
            Question(
                id=f"failed-{test.lower().replace(' ', '-')}-{tenant}",
                text=wording[test],
                kind="certificate",
                tenant=tenant,
                expected=tuple(cases),
            )
        )
    return questions


@contextmanager
def _database(database_url: URL | None) -> Iterator[tuple[URL, URL]]:
    """(owner URL, application URL) of a migrated database; a temporary one if none given."""
    settings = get_settings()
    owner = make_url(settings.migration_database_url.get_secret_value())
    temporary = database_url is None
    url = owner.set(database=_EVAL_DATABASE) if database_url is None else make_url(database_url)
    if temporary:
        if owner.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("the search eval only runs against a local Postgres")
        admin = create_engine(owner, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{_EVAL_DATABASE}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{_EVAL_DATABASE}"'))
    try:
        command.upgrade(alembic_config(url), "head")
        app = make_url(settings.database_url.get_secret_value())
        ensure_app_login(owner, app)
        yield url, url.set(username=app.username, password=app.password)
    finally:
        if temporary:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{_EVAL_DATABASE}" WITH (FORCE)'))
            admin.dispose()


def run_search_eval(
    synthetic: Path,
    coa: Path,
    recordings: Path,
    embedder: Embedder,
    *,
    pairs: int = 20,
    database_url: Any = None,
) -> SearchReport:
    questions = build_questions(synthetic, coa, pairs)
    model = get_settings().gemini_model
    with _database(database_url) as (owner_url, app_url):
        owner = create_engine(owner_url)
        with owner.begin() as conn:
            tenants = {
                name: conn.execute(
                    text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"),
                    {"n": f"search-{name}"},
                ).scalar_one()
                for name in ("a", "b", "all")
            }
        owner.dispose()
        engine = make_engine(app_url)
        sessions = make_session_factory(engine)
        parser = CachingParser(recordings / "parsed")
        provider = RecordingProvider(recordings / "llm", model)
        order_pipeline: ExtractionPipeline[PurchaseOrderExtraction] = ExtractionPipeline(
            parser, provider, PURCHASE_ORDER_SPEC
        )
        coa_pipeline: ExtractionPipeline[CoaExtraction] = ExtractionPipeline(
            parser, provider, COA_SPEC
        )
        service = DocumentService(
            sessions,
            MemoryObjectStore(),
            {
                "invoice": InvoicePipeline(parser, provider),
                "purchase_order": order_pipeline,
                "coa": coa_pipeline,
            },
            lambda session, version: None,
        )
        search = SearchService(sessions, embedder)
        keys: dict[uuid.UUID, tuple[str, str]] = {}  # document id -> (key, tenant name)
        documents = 0
        for index in range(1, pairs + 1):
            pair, case = f"pair_{index:03d}", f"coa_{index:03d}"
            files = (
                ("invoice", f"{pair}/invoice", synthetic / pair / "invoice.pdf"),
                (
                    "purchase_order",
                    f"{pair}/purchase_order",
                    synthetic / pair / "purchase_order.pdf",
                ),
                ("coa", case, coa / case / "coa.pdf"),
            )
            for doc_type, key, path in files:
                documents += 1
                for name in (_tenant(index, pairs), "all"):
                    ingested = service.ingest(
                        tenant_id=tenants[name], doc_type=doc_type, filename=f"{key}.pdf",
                        data=path.read_bytes(), actor="eval",
                    )  # fmt: skip
                    assert ingested.version is not None  # noqa: S101
                    service.process(ingested.version.id)
                    search.index_document(tenants[name], ingested.document.id)
                    keys[ingested.document.id] = (key, name)

        def ranked(tenant: str, question: Question, mode: Mode) -> list[tuple[str, str]]:
            found: list[tuple[str, str]] = []
            for hit in search.search(tenants[tenant], question.text, k=40, mode=mode):
                entry = keys[hit.document_id]
                if entry not in found:
                    found.append(entry)
            return found

        modes: dict[str, ModeResult] = {}
        for mode in MODES:
            filtered: dict[str, float] = {}
            tops: dict[str, tuple[str, ...]] = {}
            unfiltered: list[float] = []
            leaks = 0
            for question in questions:
                own = ranked(question.tenant, question, mode)
                leaks += sum(name != question.tenant for _, name in own)
                top = [key for key, _ in own][:_TOP]
                tops[question.id] = tuple(top)
                filtered[question.id] = sum(e in top for e in question.expected) / len(
                    question.expected
                )
                everything = [key for key, _ in ranked("all", question, mode)][:_TOP]
                unfiltered.append(
                    sum(e in everything for e in question.expected) / len(question.expected)
                )
            kinds = sorted({q.kind for q in questions})
            modes[mode] = ModeResult(
                recall_at_5_filtered=round(sum(filtered.values()) / len(filtered), 4),
                recall_at_5_unfiltered=round(sum(unfiltered) / len(unfiltered), 4),
                cross_tenant_hits=leaks,
                by_kind={
                    kind: round(
                        sum(filtered[q.id] for q in questions if q.kind == kind)
                        / sum(q.kind == kind for q in questions),
                        4,
                    )
                    for kind in kinds
                },
                missed=tuple(q for q, value in filtered.items() if value < 1),
                found_instead={q: tops[q] for q, value in filtered.items() if value < 1},
            )
        engine.dispose()
    return SearchReport(
        embedding_model=embedder.model, documents=documents, questions=len(questions), modes=modes
    )


def format_search_report(report: SearchReport) -> str:
    lines = [
        f"embeddings {report.embedding_model}; {report.documents} documents in two "
        f"organisations; {report.questions} questions",
    ]
    for mode, result in report.modes.items():
        kinds = ", ".join(f"{k} {v:.2f}" for k, v in result.by_kind.items())
        lines.append(
            f"{mode:8} recall@5 {result.recall_at_5_filtered:.2f} in its organisation "
            f"({kinds}); {result.recall_at_5_unfiltered:.2f} over everything; "
            f"results from the other organisation: {result.cross_tenant_hits}"
        )
    hybrid = report.modes.get("hybrid")
    if hybrid and hybrid.missed:
        lines.append(f"hybrid missed in part: {', '.join(hybrid.missed)}")
    return "\n".join(lines)
