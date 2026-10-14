"""Rules from an invoice's signed e-invoice QR: does what the IRP registered match the page?

A QR whose signature fails with a key we hold was altered: an error. One signed by a key we do
not hold is a warning, or every invoice from an IRP we hold no key for would go to review.
Only a verified QR's fields are compared: seller and buyer GSTIN, number, date and total are
errors (a genuine QR copied onto a forged invoice disagrees with it on these); the line
count, document type and the IRN printed on the page are warnings, as printing varies.
No QR, a QR that is not an e-invoice, or none readable adds no rule at all.
"""

import re
from collections.abc import Callable, Iterable
from decimal import Decimal

from docforge.einvoice.evidence import QrEvidence
from docforge.einvoice.jws import SignedInvoice
from docforge.extraction.schema import InvoiceExtraction
from docforge.parsing.base import ParsedDocument
from docforge.trust.rules import Outcome, RuleResult, Severity

TOTAL_TOLERANCE = Decimal("1.00")  # round-off on Indian invoices
_NOT_COMPARED = "A value this check needs is missing."
_SPACE = re.compile(r"[\s\-]+")
_TITLES = {"CRN": re.compile(r"credit\s*note", re.I), "DBN": re.compile(r"debit\s*note", re.I)}
_NAMES = {"INV": "an invoice", "CRN": "a credit note", "DBN": "a debit note"}

InvoiceRule = Callable[[InvoiceExtraction], Iterable[RuleResult]]


def _result(
    rule_id: str, severity: Severity, paths: tuple[str, ...], ok: bool | None, message: str
) -> RuleResult:
    outcome: Outcome = "not_evaluated" if ok is None else ("passed" if ok else "failed")
    return RuleResult(
        rule_id=rule_id,
        version=1,
        severity=severity,
        outcome=outcome,
        message=_NOT_COMPARED if ok is None else ("OK" if ok else message),
        paths=paths,
    )


def _same(printed: str | None, signed: str) -> bool | None:
    if printed is None:
        return None
    return _SPACE.sub("", printed).upper() == _SPACE.sub("", signed).upper()


def _signature(evidence: QrEvidence) -> InvoiceRule:
    severity: Severity = "warning"
    if evidence.status == "verified":
        ok, severity, message = True, "error", ""
    elif evidence.status == "bad_signature":
        ok, severity = False, "error"
        message = "The e-invoice QR's signature does not verify: the QR was altered."
    elif evidence.status == "unknown_key":
        ok = False
        message = (
            "The e-invoice QR is signed with a key DocForge does not hold, or was altered; "
            "its fields were not compared with the page."
        )
    else:
        ok = False
        message = "The e-invoice QR could not be read; its fields were not compared."

    def rule(_: InvoiceExtraction) -> Iterable[RuleResult]:
        yield _result("einvoice.signature", severity, (), ok, message)

    return rule


def _fields(signed: SignedInvoice, parsed: ParsedDocument) -> InvoiceRule:
    page = " ".join(block.text for block in parsed.blocks)
    compact_page = _SPACE.sub("", page).lower()

    def rule(invoice: InvoiceExtraction) -> Iterable[RuleResult]:
        seller = invoice.seller.gstin.value
        yield _result(
            "einvoice.seller_gstin", "error", ("seller.gstin",),
            _same(seller, signed.seller_gstin),
            f"The seller GSTIN {seller} is not the one registered: {signed.seller_gstin}.",
        )  # fmt: skip
        if signed.buyer_gstin != "URP":
            buyer = invoice.buyer.gstin.value
            yield _result(
                "einvoice.buyer_gstin", "error", ("buyer.gstin",),
                _same(buyer, signed.buyer_gstin),
                f"The buyer GSTIN {buyer} is not the one registered: {signed.buyer_gstin}.",
            )  # fmt: skip
        number = invoice.invoice_no.value
        yield _result(
            "einvoice.invoice_no", "error", ("invoice_no",), _same(number, signed.doc_no),
            f"The invoice number {number} is not the one registered: {signed.doc_no}.",
        )  # fmt: skip
        day = invoice.invoice_date.value
        yield _result(
            "einvoice.invoice_date", "error", ("invoice_date",),
            None if day is None else day == signed.doc_date,
            f"The invoice date {day} is not the one registered: {signed.doc_date}.",
        )  # fmt: skip
        total = invoice.totals.grand_total.value
        yield _result(
            "einvoice.grand_total", "error", ("totals.grand_total",),
            None if total is None else abs(total - signed.total) <= TOTAL_TOLERANCE,
            f"The grand total {total} is not the one registered: {signed.total}.",
        )  # fmt: skip
        yield _result(
            "einvoice.line_count", "warning", ("lines",),
            len(invoice.lines) == signed.item_count if invoice.lines else None,
            f"{len(invoice.lines)} lines were read; {signed.item_count} were registered.",
        )  # fmt: skip
        titled = {kind for kind, title in _TITLES.items() if title.search(page)}
        yield _result(
            "einvoice.document_type", "warning", (),
            signed.doc_type in titled if signed.doc_type in _TITLES else True,
            f"The QR registers {_NAMES[signed.doc_type]}, but the page does not say so.",
        )  # fmt: skip
        yield _result(
            "einvoice.irn_printed", "warning", (), signed.irn.lower() in compact_page,
            "The IRN in the QR is not printed on the page.",
        )  # fmt: skip

    return rule


def einvoice_rules(evidence: QrEvidence | None, parsed: ParsedDocument) -> tuple[InvoiceRule, ...]:
    """The rules for an invoice whose QR codes gave `evidence`; none when there was none."""
    if evidence is None:
        return ()
    if evidence.status == "verified" and evidence.invoice is not None:
        return _signature(evidence), _fields(evidence.invoice, parsed)
    return (_signature(evidence),)
