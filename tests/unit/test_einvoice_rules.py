"""V2 1.1 part 4: a verified e-invoice QR compared with what the invoice's page says."""

import base64
import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from docforge.einvoice.evidence import QrEvidence, evidence_from
from docforge.einvoice.keys import KeyRing, TrustedKey
from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.pipeline import INVOICE_SPEC, InvoicePipeline
from docforge.extraction.schema import InvoiceExtraction, RawInvoice
from docforge.parsing.base import BBox, Block, ParsedDocument
from docforge.review.revise import reassess
from docforge.trust.assess import Assessment, assess
from docforge.trust.einvoice_rules import einvoice_rules
from docforge.trust.invoice_rules import INVOICE_RULES
from docforge.trust.rules import RuleResult
from fakes import FakeParser, ScriptedProvider, cited

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
LABEL: dict[str, Any] = json.loads((FIXTURES / "pair_001" / "label.json").read_text("utf-8"))
OTHER: dict[str, Any] = json.loads((FIXTURES / "pair_002" / "label.json").read_text("utf-8"))
UPI = "upi://pay?pa=shop@okaxis&pn=Shop&am=100.00&cu=INR"
IRN = "3b2f" * 16
KID = "0" * 40  # the kid of the key we hold
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]

SIGNER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
STRANGER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
RING = KeyRing(
    (
        TrustedKey(
            name="TEST-1", irp="TEST", environment="production",
            source_url="https://example.test/key", fetched="2026-10-15", sha256="0" * 64,
            kid=KID, confirmed_by_real_qr=False, not_after=None,
            public_key=SIGNER.public_key(),
        ),
    )
)  # fmt: skip


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def data(label: dict[str, Any] = LABEL, **changes: Any) -> dict[str, Any]:
    """The signed fields of the invoice in `label`, as an IRP registers them."""
    invoice = label["invoice"]
    fields = {
        "SellerGstin": invoice["seller"]["gstin"], "BuyerGstin": invoice["buyer"]["gstin"],
        "DocNo": invoice["invoice_no"], "DocTyp": "INV",
        "DocDt": date.fromisoformat(invoice["invoice_date"]).strftime("%d/%m/%Y"),
        "TotInvVal": float(invoice["totals"]["grand_total"]), "ItemCnt": len(invoice["lines"]),
        "MainHsnCode": invoice["lines"][0]["hsn"], "Irn": IRN,
    }  # fmt: skip
    return fields | changes


def signed(fields: dict[str, Any], key: rsa.RSAPrivateKey = SIGNER, kid: str = KID) -> str:
    head = b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    body = b64(json.dumps({"data": json.dumps(fields), "iss": "NIC"}).encode())
    signature = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(signature)}"


def altered(qr: str) -> str:
    """The QR with its signed total changed after signing."""
    head, body, signature = qr.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["data"] = json.dumps(json.loads(payload["data"]) | {"TotInvVal": 1.0})
    return f"{head}.{b64(json.dumps(payload).encode())}.{signature}"


def page_with(parsed: ParsedDocument, *texts: str, title: str | None = None) -> ParsedDocument:
    """`texts` low on page one; `title` at its top, where a document's title is printed."""
    top = parsed.pages[0].height
    heading = (
        Block(
            id="t0", kind="text", text=title, page=1,
            bbox=BBox(x0=200, y0=top - 40, x1=400, y1=top - 25),
        ),
    ) if title else ()  # fmt: skip
    extra = tuple(
        Block(
            id=f"q{index}", kind="text", text=text, page=1,
            bbox=BBox(x0=10, y0=10 + index * 12, x1=500, y1=20 + index * 12),
        )
        for index, text in enumerate(texts)
    )  # fmt: skip
    return parsed.model_copy(update={"blocks": (*heading, *parsed.blocks, *extra)})


class Invoice:
    """pair_001 read perfectly, its page optionally carrying more text (an IRN, a title)."""

    def __init__(self, build: RawFromLabel, *page_text: str, title: str | None = None) -> None:
        self.raw = build(LABEL)
        self.parsed = page_with(cited(LABEL, "invoice", self.raw), *page_text, title=title)
        self.extraction = normalize_invoice(RawInvoice.model_validate(self.raw), self.parsed)

    def assess(self, *qrs: str) -> Assessment:
        evidence = evidence_from(qrs, RING)
        rules = (*INVOICE_RULES, *einvoice_rules(evidence, self.parsed))
        return assess(self.extraction, self.parsed, rules)


@pytest.fixture
def invoice(raw_invoice_from_label: RawFromLabel) -> Invoice:
    return Invoice(raw_invoice_from_label, f"IRN: {IRN}")


def outcomes(assessment: Assessment) -> dict[str, tuple[str, str]]:
    return {
        r.rule_id: (r.outcome, r.severity)
        for r in assessment.rules
        if r.rule_id.startswith("einvoice.")
    }


def failed(assessment: Assessment) -> set[str]:
    return {rule for rule, (outcome, _) in outcomes(assessment).items() if outcome != "passed"}


# --- which QR is the evidence ---------------------------------------------------------------


def test_no_qr_and_qrs_that_are_not_signed_give_no_evidence() -> None:
    assert evidence_from((), RING) is None
    assert evidence_from((UPI, "https://example.com/a.b.c", "www.example.com"), RING) is None


def test_a_verified_qr_is_the_evidence_with_its_signed_fields() -> None:
    evidence = evidence_from((UPI, signed(data())), RING)

    assert evidence is not None
    assert (evidence.status, evidence.irp, evidence.key_confirmed) == ("verified", "TEST", False)
    assert evidence.key_name == "TEST-1"
    assert evidence.invoice is not None
    assert evidence.invoice.doc_no == LABEL["invoice"]["invoice_no"]
    assert evidence.qr_index == 1


def test_a_verified_qr_wins_over_a_forged_one_beside_it() -> None:
    evidence = evidence_from((altered(signed(data())), signed(data())), RING)

    assert evidence is not None and evidence.status == "verified"


def test_an_unverified_qr_carries_no_signed_fields() -> None:
    evidence = evidence_from((signed(data(), STRANGER, "F" * 40),), RING)

    assert evidence is not None and evidence.status == "unknown_key"
    assert (evidence.invoice, evidence.irp, evidence.key_name) == (None, None, None)


def test_evidence_survives_storage_unchanged() -> None:
    fresh = evidence_from((signed(data(OTHER)),), RING)

    assert fresh is not None
    assert QrEvidence.model_validate_json(fresh.model_dump_json()) == fresh


# --- the rules ------------------------------------------------------------------------------


def test_a_genuine_qr_whose_fields_match_the_page_changes_nothing(invoice: Invoice) -> None:
    before = assess(invoice.extraction, invoice.parsed, INVOICE_RULES)
    after = invoice.assess(signed(data()))

    assert (after.decision, after.reasons) == (before.decision, before.reasons) == ("accept", ())
    assert failed(after) == set()
    assert set(outcomes(after)) == {
        "einvoice.signature", "einvoice.seller_gstin", "einvoice.buyer_gstin",
        "einvoice.invoice_no", "einvoice.invoice_date", "einvoice.grand_total",
        "einvoice.line_count", "einvoice.document_type", "einvoice.irn_printed",
    }  # fmt: skip


def test_no_qr_or_a_upi_qr_adds_no_rule(invoice: Invoice) -> None:
    assert outcomes(invoice.assess()) == {}
    assert outcomes(invoice.assess(UPI)) == {}


def test_an_altered_qr_sends_the_invoice_to_review(invoice: Invoice) -> None:
    result = invoice.assess(altered(signed(data())))

    assert result.decision == "review"
    assert outcomes(result) == {"einvoice.signature": ("failed", "error")}
    assert "einvoice.signature" in " ".join(result.reasons)


def test_a_qr_signed_by_a_key_we_do_not_hold_is_a_warning_only(invoice: Invoice) -> None:
    result = invoice.assess(signed(data(), STRANGER, "F" * 40))

    assert result.decision == "accept"
    assert outcomes(result) == {"einvoice.signature": ("failed", "warning")}


def test_an_altered_qr_naming_a_kid_we_do_not_hold_is_only_a_warning(invoice: Invoice) -> None:
    # Without the kid of the key that signed it, an altered QR and one signed by a key we lack
    # look the same. NIC publishes bare keys; their kids are recorded from real QRs (part 7).
    result = invoice.assess(altered(signed(data(), kid="E" * 40)))

    assert outcomes(result) == {"einvoice.signature": ("failed", "warning")}


def test_an_altered_qr_beside_a_verified_one_is_still_an_error(invoice: Invoice) -> None:
    result = invoice.assess(signed(data()), altered(signed(data(OTHER))))

    assert outcomes(result)["einvoice.signature"] == ("failed", "error")
    assert result.decision == "review"
    assert failed(result) == {"einvoice.signature"}  # the verified QR's fields still compared


def test_a_missing_signed_field_on_the_page_forces_review(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    def without_total(label: dict[str, Any]) -> dict[str, Any]:
        raw = raw_invoice_from_label(label)
        raw["totals"]["grand_total"] = {"text": None, "block_ids": []}
        return raw

    result = Invoice(without_total, f"IRN: {IRN}").assess(signed(data()))

    assert outcomes(result)["einvoice.grand_total"] == ("not_evaluated", "error")
    assert result.decision == "review"


def test_a_genuine_qr_copied_from_another_invoice_is_caught(invoice: Invoice) -> None:
    result = invoice.assess(signed(data(OTHER)))

    assert result.decision == "review"
    assert outcomes(result)["einvoice.signature"] == ("passed", "error")
    assert {
        "einvoice.seller_gstin", "einvoice.buyer_gstin", "einvoice.invoice_no",
        "einvoice.invoice_date", "einvoice.grand_total",
    } <= failed(result)  # fmt: skip


@pytest.mark.parametrize(
    ("change", "rule", "path"),
    [
        ({"SellerGstin": "27AAKCS4821M1ZW"}, "einvoice.seller_gstin", "seller.gstin"),
        ({"BuyerGstin": "27AAVFS2087J1ZF"}, "einvoice.buyer_gstin", "buyer.gstin"),
        ({"DocNo": "NVM/26-27/32002"}, "einvoice.invoice_no", "invoice_no"),
        ({"DocDt": "03/09/2026"}, "einvoice.invoice_date", "invoice_date"),
        ({"TotInvVal": 98699.01}, "einvoice.grand_total", "totals.grand_total"),
    ],
)
def test_each_signed_field_that_disagrees_is_an_error_on_its_field(
    invoice: Invoice, change: dict[str, Any], rule: str, path: str
) -> None:
    result = invoice.assess(signed(data(**change)))

    assert result.decision == "review"
    assert failed(result) == {rule}
    (field,) = [f for f in result.fields if f.path == path]
    assert field.needs_review and f"failed {rule}" in field.reasons


def test_the_total_may_differ_by_round_off_up_to_one_rupee(invoice: Invoice) -> None:
    grand = Decimal(LABEL["invoice"]["totals"]["grand_total"])

    near = invoice.assess(signed(data(TotInvVal=float(grand + Decimal("1.00")))))
    far = invoice.assess(signed(data(TotInvVal=float(grand + Decimal("1.01")))))

    assert "einvoice.grand_total" not in failed(near)
    assert "einvoice.grand_total" in failed(far)


def test_case_does_not_make_an_invoice_number_disagree(invoice: Invoice) -> None:
    fields = data(DocNo=LABEL["invoice"]["invoice_no"].lower())

    assert failed(invoice.assess(signed(fields))) == set()


def test_a_hyphen_or_slash_makes_an_invoice_number_different(invoice: Invoice) -> None:
    number = LABEL["invoice"]["invoice_no"]  # NVM/26-27/32001

    for other in (number.replace("-", ""), number.replace("/", "-")):
        assert failed(invoice.assess(signed(data(DocNo=other)))) == {"einvoice.invoice_no"}


def test_an_unregistered_buyer_beside_a_printed_buyer_gstin_is_a_warning(
    invoice: Invoice, raw_invoice_from_label: RawFromLabel
) -> None:
    def no_buyer_gstin(label: dict[str, Any]) -> dict[str, Any]:
        raw = raw_invoice_from_label(label)
        raw["buyer"]["gstin"] = {"text": None, "block_ids": []}
        return raw

    printed = invoice.assess(signed(data(BuyerGstin="URP")))
    unprinted = Invoice(no_buyer_gstin, f"IRN: {IRN}").assess(signed(data(BuyerGstin="URP")))

    assert outcomes(printed)["einvoice.buyer_gstin"] == ("failed", "warning")
    assert printed.decision == "accept"
    assert outcomes(unprinted)["einvoice.buyer_gstin"] == ("passed", "warning")


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"ItemCnt": 11}, "einvoice.line_count"),
        ({"DocTyp": "CRN"}, "einvoice.document_type"),
        ({"Irn": "c" * 64}, "einvoice.irn_printed"),
    ],
)
def test_line_count_document_type_and_irn_are_warnings(
    invoice: Invoice, change: dict[str, Any], rule: str
) -> None:
    result = invoice.assess(signed(data(**change)))

    assert result.decision == "accept"
    assert outcomes(result)[rule] == ("failed", "warning")


def test_a_credit_note_whose_page_says_so_passes(raw_invoice_from_label: RawFromLabel) -> None:
    note = Invoice(raw_invoice_from_label, f"IRN: {IRN}", title="CREDIT NOTE")

    assert failed(note.assess(signed(data(DocTyp="CRN")))) == set()


def test_credit_note_only_in_the_terms_is_not_a_title(raw_invoice_from_label: RawFromLabel) -> None:
    terms = "Any credit note against this bill will be issued within 7 days."
    invoice = Invoice(raw_invoice_from_label, terms, f"IRN: {IRN}", title="TAX INVOICE")

    assert failed(invoice.assess(signed(data(DocTyp="CRN")))) == {"einvoice.document_type"}
    assert failed(invoice.assess(signed(data()))) == set()


def test_an_invoice_qr_on_a_page_titled_credit_note_is_a_warning(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    note = Invoice(raw_invoice_from_label, f"IRN: {IRN}", title="Credit Note")

    result = note.assess(signed(data()))

    assert outcomes(result)["einvoice.document_type"] == ("failed", "warning")


def test_an_irn_split_over_lines_on_the_page_is_found(raw_invoice_from_label: RawFromLabel) -> None:
    split = Invoice(raw_invoice_from_label, f"IRN : {IRN[:32]}", IRN[32:].upper())

    assert failed(split.assess(signed(data()))) == set()


def test_a_value_the_page_lacks_is_not_evaluated_not_guessed(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    def without_number(label: dict[str, Any]) -> dict[str, Any]:
        raw = raw_invoice_from_label(label)
        raw["invoice_no"] = {"text": None, "block_ids": []}
        return raw

    result = Invoice(without_number, f"IRN: {IRN}").assess(signed(data()))

    assert outcomes(result)["einvoice.invoice_no"] == ("not_evaluated", "error")


def test_every_einvoice_rule_has_a_version_and_a_message(invoice: Invoice) -> None:
    results = [
        r for r in invoice.assess(signed(data(OTHER))).rules if r.rule_id.startswith("einvoice.")
    ]

    assert results and all(r.version >= 1 and r.message for r in results)


# --- passed through reading and review ------------------------------------------------------


def _extra(rule_id: str) -> Callable[[InvoiceExtraction], list[RuleResult]]:
    def rule(_: InvoiceExtraction) -> list[RuleResult]:
        return [
            RuleResult(
                rule_id=rule_id, version=1, severity="error", outcome="failed",
                message="extra", paths=("invoice_no",),
            )
        ]  # fmt: skip

    return rule


def test_reading_runs_extra_rules_after_the_document_types_own(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    def pipeline() -> InvoicePipeline:
        reply = json.dumps(raw_invoice_from_label(LABEL))
        return InvoicePipeline(FakeParser(), ScriptedProvider([reply]))

    parsed = FakeParser().parse(b"%PDF")

    plain = pipeline().extract(parsed)
    extra = pipeline().extract(parsed, extra_rules=(_extra("einvoice.test"),))

    assert "einvoice.test" not in {r.rule_id for r in plain.assessment.rules}
    assert extra.assessment.rules[-1].rule_id == "einvoice.test"
    assert extra.assessment.decision == "review"


def test_review_reassesses_with_the_same_extra_rules(raw_invoice_from_label: RawFromLabel) -> None:
    printed = raw_invoice_from_label(LABEL)
    parsed = cited(LABEL, "invoice", printed)
    raw = RawInvoice.model_validate(printed)

    plain = reassess(INVOICE_SPEC, raw, parsed, [])
    extra = reassess(INVOICE_SPEC, raw, parsed, [], extra_rules=(_extra("einvoice.test"),))
    with pytest.raises(TypeError):
        reassess(INVOICE_SPEC, raw, parsed, [], (_extra("einvoice.test"),))  # type: ignore[misc]

    assert plain.assessment.decision == "accept"
    assert extra.assessment.decision == "review"
    assert extra.assessment.rules[-1].rule_id == "einvoice.test"
