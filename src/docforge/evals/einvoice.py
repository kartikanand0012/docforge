"""E-invoice QRs on the clean invoices: are forged ones caught, and is nothing else changed?

Each clean invoice, read from recordings, is assessed five times with a QR beside it:
- genuine: its own fields, signed with a key we hold. It should change nothing - except where
  the model misread a signed field, which the QR then catches;
- copied: another invoice's genuine QR. Its number and total must be caught;
- altered: its own QR with the total changed after signing. The signature must fail;
- unknown key: its own fields signed with a key we do not hold, and a UPI QR. Neither proves
  anything, so neither may change the decision.

The keys are made for this run and never stored; the report holds no QR text.
"""

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel, ConfigDict

from docforge.einvoice.evidence import evidence_from
from docforge.einvoice.keys import KeyRing, TrustedKey
from docforge.extraction.pipeline import INVOICE_SPEC, ExtractionPipeline
from docforge.extraction.schema import InvoiceExtraction
from docforge.synth.models import Invoice, PairLabel
from docforge.trust.assess import Assessment, assess
from docforge.trust.einvoice_rules import einvoice_rules

Kind = Literal["genuine", "copied", "altered", "unknown_key", "upi"]
_HELD_KID, _OTHER_KID = "E" * 40, "F" * 40
_UPI = "upi://pay?pa=shop@okaxis&pn=Shop&am=100.00&cu=INR"
_MUST_CATCH: dict[Kind, set[str]] = {
    "copied": {"einvoice.invoice_no", "einvoice.grand_total"},
    "altered": {"einvoice.signature"},
}


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EinvoiceCase(_Model):
    pair_id: str
    kind: Kind
    decision_without_qr: str
    decision: str
    findings: tuple[str, ...]  # e-invoice rules that did not pass, as "rule_id (severity)"
    expected_met: bool


class EinvoiceSummary(_Model):
    pairs: int
    genuine_unchanged: int  # a genuine QR left the decision as it was
    forged_cases: int
    forged_caught: int  # a copied or altered QR caught and sent to review
    harmless_cases: int
    harmless_unchanged: int  # a UPI QR or an unknown key left the decision as it was


class EinvoiceReport(_Model):
    provider: str = "gemini"
    model: str
    invoice_prompt_version: str
    summary: EinvoiceSummary
    cases: tuple[EinvoiceCase, ...]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _fields(invoice: Invoice) -> dict[str, Any]:
    irn = hashlib.sha256(f"{invoice.seller.gstin}{invoice.invoice_no}".encode()).hexdigest()
    return {
        "SellerGstin": invoice.seller.gstin, "BuyerGstin": invoice.buyer.gstin,
        "DocNo": invoice.invoice_no, "DocTyp": "INV",
        "DocDt": invoice.invoice_date.strftime("%d/%m/%Y"),
        "TotInvVal": float(invoice.totals.grand_total), "ItemCnt": len(invoice.lines),
        "MainHsnCode": invoice.lines[0].hsn if invoice.lines else None, "Irn": irn,
    }  # fmt: skip


def _signed(fields: dict[str, Any], key: rsa.RSAPrivateKey, kid: str) -> str:
    head = _b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    body = _b64(json.dumps({"data": json.dumps(fields), "iss": "EVAL"}).encode())
    signature = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(signature)}"


def _altered(qr: str) -> str:
    head, body, signature = qr.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["data"] = json.dumps(json.loads(payload["data"]) | {"TotInvVal": 1})
    return f"{head}.{_b64(json.dumps(payload).encode())}.{signature}"


def _findings(assessment: Assessment) -> tuple[str, ...]:
    return tuple(
        f"{rule.rule_id} ({rule.severity})"
        for rule in assessment.rules
        if rule.rule_id.startswith("einvoice.") and rule.outcome != "passed"
    )


def run_einvoice_eval(
    clean_dir: Path, invoices: ExtractionPipeline[InvoiceExtraction]
) -> EinvoiceReport:
    signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ring = KeyRing(
        (
            TrustedKey(
                name="EVAL-ONLY", irp="EVAL", environment="sandbox", source_url="none",
                fetched="2026-10-15", sha256="0" * 64, kid=_HELD_KID,
                confirmed_by_real_qr=False, not_after=None, public_key=signer.public_key(),
            ),
        )
    )  # fmt: skip
    labels = [
        PairLabel.model_validate_json((directory / "label.json").read_text("utf-8"))
        for directory in sorted(clean_dir.glob("pair_*"))
    ]
    if len(labels) < 2:
        raise ValueError("the copied case needs at least two invoices")
    cases: list[EinvoiceCase] = []
    for index, label in enumerate(labels):
        read = invoices.extract(
            invoices.parser.parse((clean_dir / label.pair_id / "invoice.pdf").read_bytes())
        )
        own = _signed(_fields(label.invoice), signer, _HELD_KID)
        other = labels[(index + 1) % len(labels)].invoice
        qrs: dict[Kind, str] = {
            "genuine": own,
            "copied": _signed(_fields(other), signer, _HELD_KID),
            "altered": _altered(own),
            "unknown_key": _signed(_fields(label.invoice), stranger, _OTHER_KID),
            "upi": _UPI,
        }
        for kind, qr in qrs.items():
            rules = einvoice_rules(evidence_from((qr,), ring), read.parsed)
            assessment = assess(read.extraction, read.parsed, (*INVOICE_SPEC.rules, *rules))
            failed = {r.rule_id for r in assessment.rules if r.outcome == "failed"}
            errors = any(
                r.rule_id.startswith("einvoice.") and r.severity == "error"
                and r.outcome != "passed"
                for r in assessment.rules
            )  # fmt: skip
            if kind in _MUST_CATCH:
                met = assessment.decision == "review" and _MUST_CATCH[kind] <= failed
            else:
                met = assessment.decision == read.assessment.decision and not errors
            cases.append(
                EinvoiceCase(
                    pair_id=label.pair_id,
                    kind=kind,
                    decision_without_qr=read.assessment.decision,
                    decision=assessment.decision,
                    findings=_findings(assessment),
                    expected_met=met,
                )
            )

    def count(*kinds: Kind, met: bool | None = None) -> int:
        return sum(c.kind in kinds and met in (None, c.expected_met) for c in cases)

    return EinvoiceReport(
        model=invoices.provider.model,
        invoice_prompt_version=INVOICE_SPEC.prompt_version,
        summary=EinvoiceSummary(
            pairs=len(labels),
            genuine_unchanged=count("genuine", met=True),
            forged_cases=count("copied", "altered"),
            forged_caught=count("copied", "altered", met=True),
            harmless_cases=count("unknown_key", "upi"),
            harmless_unchanged=count("unknown_key", "upi", met=True),
        ),
        cases=tuple(cases),
    )


def format_einvoice_report(report: EinvoiceReport) -> str:
    s = report.summary
    lines = [
        f"model {report.model}; prompt {report.invoice_prompt_version}",
        f"forged QRs caught: {s.forged_caught} of {s.forged_cases}",
        f"UPI QRs and unknown keys: {s.harmless_unchanged} of {s.harmless_cases} unchanged",
        f"genuine QRs: {s.genuine_unchanged} of {s.pairs} unchanged",
    ]
    lines += [
        f"  {case.kind} {case.pair_id}: {case.decision_without_qr} -> {case.decision}, "
        f"{list(case.findings)}"
        for case in report.cases
        if not case.expected_met
    ]
    return "\n".join(lines)
