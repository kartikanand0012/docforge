"""The held-out search set: questions in forms the search was never tuned on.

The C7 search eval's questions came from the same templates the search was tuned against, so
its recall is an upper bound. These are written differently and scored by rank:

- `ocr_code`: a batch number with one character misread as OCR does (0/O, 1/I, 5/S, 8/B);
- `code_variant`: an invoice number written another way (spaces for slashes, lower case);
- `natural`: everyday wording, which may fit several near-identical documents (all count);
- `multi`: a supplier's GSTIN, which names every invoice from them;
- `no_answer`: codes printed on no document; the right answer is nothing.
"""

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from docforge.evals.search import _TOP, MODES, _label, _tenant, indexed_corpus
from docforge.search.embeddings import Embedder

_MISREAD = {"0": "O", "O": "0", "1": "I", "I": "1", "5": "S", "S": "5", "8": "B", "B": "8"}
_DEPTH = 40  # results looked at for the reciprocal rank
_UNPRINTED = ("QZX770011", "KWP552019", "ZZT108843", "JHQ660027", "VXR331905")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class HeldoutQuestion(_Model):
    id: str
    text: str
    kind: str
    tenant: str
    expected: tuple[str, ...]  # empty when nothing should be found


class HeldoutMode(_Model):
    hit_at_1: float
    recall_at_5: float
    mrr: float
    by_kind: dict[str, dict[str, float]]
    abstained_when_no_answer: float  # share of unanswerable questions given no results
    cross_tenant_hits: int
    missed: tuple[str, ...]


class HeldoutReport(_Model):
    embedding_model: str
    documents: int
    questions: int
    modes: dict[str, HeldoutMode]


def _misread(code: str) -> str | None:
    """The code with its last confusable character misread, or None if it has none."""
    for at in range(len(code) - 1, -1, -1):
        if code[at] in _MISREAD:
            return code[:at] + _MISREAD[code[at]] + code[at + 1 :]
    return None


def _generic(product: str) -> str:
    return re.split(r"\s", product)[0].lower()


def build_heldout_questions(synthetic: Path, coa: Path, pairs: int = 20) -> list[HeldoutQuestion]:
    questions: list[HeldoutQuestion] = []
    invoices: list[tuple[str, str, dict[str, Any]]] = []  # (pair, tenant, invoice label)
    for index in range(1, pairs + 1):
        pair = f"pair_{index:03d}"
        label = _label(synthetic / pair / "label.json")["invoice"]
        invoices.append((pair, _tenant(index, pairs), label))

    printed = json.dumps(
        [_label(p) for p in sorted(synthetic.glob("*/label.json"))]
        + [_label(p) for p in sorted(coa.glob("*/label.json"))]
    )
    for pair, tenant, invoice in invoices:
        key = f"{pair}/invoice"
        misread = _misread(invoice["lines"][0]["batch_no"])
        if misread is not None and misread not in printed:
            questions.append(
                HeldoutQuestion(
                    id=f"{pair}-ocr-batch",
                    text=f"stock received under batch {misread}",
                    kind="ocr_code",
                    tenant=tenant,
                    expected=(key,),
                )
            )
        variant = invoice["invoice_no"].replace("/", " ").lower()
        questions.append(
            HeldoutQuestion(
                id=f"{pair}-invoice-variant",
                text=f"find bill {variant}",
                kind="code_variant",
                tenant=tenant,
                expected=(key,),
            )
        )
        generic = _generic(invoice["lines"][0]["product_name"])
        seller = invoice["seller"]["name"]
        alike = tuple(
            f"{other}/invoice"
            for other, other_tenant, label in invoices
            if other_tenant == tenant
            and label["seller"]["name"] == seller
            and _generic(label["lines"][0]["product_name"]) == generic
        )
        questions.append(
            HeldoutQuestion(
                id=f"{pair}-natural",
                text=f"the {generic} bill we got from {seller}",
                kind="natural",
                tenant=tenant,
                expected=alike,
            )
        )

    suppliers: dict[tuple[str, str], list[str]] = {}
    for pair, tenant, invoice in invoices:
        suppliers.setdefault((tenant, invoice["seller"]["gstin"]), []).append(f"{pair}/invoice")
    for (tenant, gstin), keys in sorted(suppliers.items()):
        questions.append(
            HeldoutQuestion(
                id=f"gstin-{gstin}-{tenant}",
                text=f"everything we were billed by {gstin}",
                kind="multi",
                tenant=tenant,
                expected=tuple(keys),
            )
        )

    for index in range(1, pairs + 1):
        case = f"coa_{index:03d}"
        label = _label(coa / case / "label.json")["coa"]
        questions.append(
            HeldoutQuestion(
                id=f"{case}-natural",
                text=f"quality report for {label['product_name']} lot {label['batch_no']}",
                kind="natural",
                tenant=_tenant(index, pairs),
                expected=(case,),
            )
        )

    for number, code in enumerate(_UNPRINTED):
        if code in printed:
            continue
        for tenant in ("a", "b"):
            questions.append(
                HeldoutQuestion(
                    id=f"none-{number}-{tenant}",
                    text=f"invoice for batch {code}",
                    kind="no_answer",
                    tenant=tenant,
                    expected=(),
                )
            )
    return questions


def run_heldout_eval(
    synthetic: Path,
    coa: Path,
    recordings: Path,
    embedder: Embedder,
    *,
    pairs: int = 20,
    database_url: Any = None,
) -> HeldoutReport:
    questions = build_heldout_questions(synthetic, coa, pairs)
    answerable = [q for q in questions if q.expected]
    with indexed_corpus(
        synthetic, coa, recordings, embedder, pairs=pairs, database_url=database_url
    ) as corpus:
        modes: dict[str, HeldoutMode] = {}
        for mode in MODES:
            at1: dict[str, float] = {}
            at5: dict[str, float] = {}
            reciprocal: dict[str, float] = {}
            abstained: list[bool] = []
            leaks = 0
            for question in questions:
                found: list[str] = []
                for hit in corpus.search.search(
                    corpus.tenants[question.tenant], question.text, k=_DEPTH, mode=mode
                ):
                    key, tenant = corpus.keys[hit.document_id]
                    leaks += tenant != question.tenant
                    if key not in found:
                        found.append(key)
                if not question.expected:
                    abstained.append(not found)
                    continue
                expected = set(question.expected)
                at1[question.id] = float(bool(found) and found[0] in expected)
                at5[question.id] = len(expected & set(found[:_TOP])) / min(_TOP, len(expected))
                rank = next((n for n, key in enumerate(found, 1) if key in expected), None)
                reciprocal[question.id] = 1 / rank if rank else 0.0
            modes[mode] = HeldoutMode(
                hit_at_1=_mean(answerable, at1),
                recall_at_5=_mean(answerable, at5),
                mrr=_mean(answerable, reciprocal),
                by_kind={
                    kind: {
                        "recall_at_5": _mean(answerable, at5, kind),
                        "mrr": _mean(answerable, reciprocal, kind),
                    }
                    for kind in sorted({q.kind for q in answerable})
                },
                abstained_when_no_answer=(
                    round(sum(abstained) / len(abstained), 4) if abstained else 0.0
                ),
                cross_tenant_hits=leaks,
                missed=tuple(q for q, value in at5.items() if value < 1),
            )
        documents = corpus.documents
    return HeldoutReport(
        embedding_model=embedder.model,
        documents=documents,
        questions=len(questions),
        modes=modes,
    )


def _mean(
    questions: list[HeldoutQuestion], values: dict[str, float], kind: str | None = None
) -> float:
    chosen = [values[q.id] for q in questions if kind is None or q.kind == kind]
    return round(sum(chosen) / len(chosen), 4) if chosen else 0.0


def format_heldout_report(report: HeldoutReport) -> str:
    lines = [f"held-out: {report.questions} questions over {report.documents} documents"]
    for mode, result in report.modes.items():
        kinds = ", ".join(f"{k} {v['recall_at_5']:.2f}" for k, v in result.by_kind.items())
        lines.append(
            f"{mode:8} hit@1 {result.hit_at_1:.2f} recall@5 {result.recall_at_5:.2f} "
            f"MRR {result.mrr:.2f} ({kinds}); nothing returned for "
            f"{result.abstained_when_no_answer:.0%} of unanswerable; "
            f"other organisation: {result.cross_tenant_hits}"
        )
    return "\n".join(lines)
