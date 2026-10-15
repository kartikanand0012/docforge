"""An invoice's e-invoice check as the review and assessment responses show it.

The signed QR itself is never sent: its outcome, who signed it and what it signed are enough to
show and to act on, and the JWS is kept only to be checked again later.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from docforge.einvoice.view import EinvoiceView


class SignedInvoiceOut(BaseModel):
    """What the IRP signed, as the QR states it."""

    seller_gstin: str
    buyer_gstin: str  # "URP" for an unregistered buyer
    invoice_no: str
    document_type: str  # INV, CRN (credit note) or DBN (debit note)
    invoice_date: date
    total: Decimal
    line_count: int
    main_hsn: str | None
    irn: str  # the invoice reference number the IRP gave it
    irn_date: str | None  # as the IRP wrote it


class EinvoiceRuleOut(BaseModel):
    rule_id: str  # einvoice.signature, einvoice.invoice_no, ...
    outcome: str  # passed, failed or not_evaluated
    severity: str  # error or warning
    message: str


class EinvoiceOut(BaseModel):
    """`none`: no signed QR on it; `unreadable`: its pages could not be scanned. A key that is
    not `key_confirmed` has not yet verified a real invoice's QR."""

    status: Literal["verified", "bad_signature", "malformed", "unknown_key", "none", "unreadable"]
    reason: str | None  # why it is not verified
    irp: str | None  # the registration portal whose key verified it
    key_name: str | None
    key_confirmed: bool
    page: int | None  # where the QR was found
    altered_beside: bool  # another QR on the invoice was altered
    signed: SignedInvoiceOut | None  # only when verified
    rules: list[EinvoiceRuleOut]  # the checks against the page, as of this assessment


def einvoice_out(view: EinvoiceView | None) -> EinvoiceOut | None:
    """The check as sent; null when the version was never scanned."""
    if view is None:
        return None
    signed = view.signed
    return EinvoiceOut(
        status=view.status,  # validated here against the Literal
        reason=view.reason,
        irp=view.irp,
        key_name=view.key_name,
        key_confirmed=view.key_confirmed,
        page=view.page,
        altered_beside=view.altered_beside,
        signed=None
        if signed is None
        else SignedInvoiceOut(
            seller_gstin=signed.seller_gstin,
            buyer_gstin=signed.buyer_gstin,
            invoice_no=signed.doc_no,
            document_type=signed.doc_type,
            invoice_date=signed.doc_date,
            total=signed.total,
            line_count=signed.item_count,
            main_hsn=signed.main_hsn,
            irn=signed.irn,
            irn_date=signed.irn_date,
        ),
        rules=[
            EinvoiceRuleOut(
                rule_id=rule.rule_id,
                outcome=rule.outcome,
                severity=rule.severity,
                message=rule.message,
            )
            for rule in view.rules
        ],
    )
