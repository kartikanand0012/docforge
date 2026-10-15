"""What an invoice's stored e-invoice check shows a person: its outcome, who signed the QR, what
it signed and which of its checks hold - never the QR itself."""

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from docforge.db.models import EinvoiceCheck
from docforge.einvoice.evidence import QrEvidence
from docforge.einvoice.jws import SignedInvoice
from docforge.trust.rules import RuleResult

logger = logging.getLogger(__name__)

RULE_PREFIX = "einvoice."
UNREADABLE_EVIDENCE = "the stored check could not be read"


@dataclass(frozen=True)
class EinvoiceView:
    status: str  # verified, bad_signature, malformed, unknown_key, none or unreadable
    reason: str | None  # why it is not verified, or that the stored check cannot be read
    irp: str | None  # the IRP whose key verified it
    key_name: str | None
    key_confirmed: bool  # that key has verified a real invoice's QR
    page: int | None
    altered_beside: bool  # another QR on the invoice was altered
    signed: SignedInvoice | None  # only when verified
    rules: tuple[RuleResult, ...]  # the e-invoice rules of the assessment shown with it


def stored_check(session: Session, version_id: uuid.UUID) -> EinvoiceCheck | None:
    """The version's check, without its signed QR: nothing shown or reassessed needs it."""
    return session.scalar(
        select(EinvoiceCheck)
        .where(EinvoiceCheck.document_version_id == version_id)
        .options(defer(EinvoiceCheck.qr_text))
    )


def einvoice_view(check: EinvoiceCheck | None, rules: Iterable[RuleResult]) -> EinvoiceView | None:
    """The check as shown beside an assessment; None when the version was never scanned."""
    if check is None:
        return None
    evidence, reason = None, None
    if check.evidence is not None:
        try:
            evidence = QrEvidence.model_validate(check.evidence)
        except ValidationError:
            logger.warning("stored e-invoice check %s cannot be read", check.id)
            reason = UNREADABLE_EVIDENCE
    if evidence is not None and evidence.status != "verified":
        reason = evidence.reason
    return EinvoiceView(
        status=check.status,
        reason=reason,
        irp=evidence.irp if evidence else None,
        key_name=evidence.key_name if evidence else None,
        key_confirmed=evidence.key_confirmed if evidence else False,
        page=check.page,
        altered_beside=evidence.altered_beside if evidence else False,
        signed=evidence.invoice if evidence else None,
        rules=tuple(rule for rule in rules if rule.rule_id.startswith(RULE_PREFIX)),
    )
