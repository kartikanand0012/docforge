"""What the audit-log screen may show of each action: its label, and the details it may show.

An allow-list: a detail not named here is not sent to the screen or the export (the entry
says how many were held back), so a detail added later fails closed until someone decides it
may be seen. The log itself keeps every detail; its hashes are always checked whole.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Action:
    label: str
    shown: tuple[str, ...] = ()


ACTIONS: dict[str, Action] = {
    # Documents, as the worker reads them
    "document.received": Action(
        "Document received", ("doc_type", "media_type", "size_bytes", "sha256")
    ),
    "document.reprocess_requested": Action("Reading requested again", ("version_no",)),
    "processing.started": Action("Reading started", ("version_no", "attempt", "stage")),
    "processing.stage": Action("Stage reached", ("stage",)),
    "extraction.created": Action(
        "Fields read", ("version_no", "extraction_sha256", "model", "model_calls")
    ),
    "assessment.created": Action(
        "Checks run", ("version_no", "decision", "values_flagged", "checks_failed")
    ),
    "einvoice.checked": Action("E-invoice QR checked", ("version_no", "status", "irp")),
    "match.created": Action("Matched", ("counterpart_document_id", "decision", "discrepancies")),
    "match.linked": Action(
        "Linked to its order by hand", ("counterpart_document_id", "decision", "discrepancies")
    ),
    "processing.retry_scheduled": Action(
        "Reading to be retried", ("version_no", "attempt", "error")
    ),
    "processing.failed": Action("Reading failed", ("version_no", "error")),
    "indexing.completed": Action("Ready to search and ask", ("version_no",)),
    "document.deleted": Action("Document deleted"),
    # People
    "account.created": Action("Account made"),
    "platform.workspace_viewed": Action("Workspace viewed by the platform administrator"),
    "reviewer.pin_failed": Action("Wrong PIN", ("attempt", "at")),
    "review.corrected": Action("Value corrected", ("path", "version_no", "changed")),
    "review.signed": Action(
        "Review signed", ("outcome", "version_no", "record_sha256", "overridden")
    ),
    "review.claimed": Action("Review started", ("version_no",)),
    "review.taken_over": Action("Review taken over", ("from_reviewer",)),
    # Questions (never the question or its answer)
    "chat.question_asked": Action("Question asked"),
    # Knowledge bases
    "collection.created": Action("Knowledge base made", ("name",)),
    "collection.renamed": Action("Knowledge base renamed", ("name",)),
    "collection.deleted": Action("Knowledge base deleted", ("name",)),
    "collection.documents_added": Action("Documents added to a knowledge base", ("count",)),
    "collection.documents_removed": Action("Documents removed from a knowledge base", ("count",)),
    # Workspace settings
    "settings.changed": Action("Setting changed", ("setting", "old", "new")),
    # Schedule H1 products a reviewer marks for the workspace
    "h1_product.marked": Action("Product marked Schedule H1", ("product_name",)),
    "h1_product.unmarked": Action("Schedule H1 mark removed", ("product_name",)),
    # The mapping master (V2 P1): what was done, never the names themselves
    "mapping.created": Action("Mapping made", ("kind", "source")),
    "mapping.updated": Action("Mapping changed", ("kind", "fields")),
    "mapping.deleted": Action("Mapping removed", ("kind",)),
    "mapping.imported": Action(
        "Master names imported", ("rows", "imported", "already_known", "rejected")
    ),
    # Export runs for Tally and Marg
    "export.created": Action(
        "Export run",
        (
            "target",
            "status",
            "invoices",
            "lines",
            "unmapped_suppliers",
            "unmapped_items",
            "problems",
            "sha256",
            "format",
            "profile_version",
            "exported_before",
        ),
    ),
    "export.downloaded": Action("Export file downloaded", ("sha256",)),
    # The purchase register (V2 P4): counts and the file's hash, never a value from a bill
    "register.exported": Action(
        "Purchase register exported",
        ("month", "invoices", "supplier_filter", "with_2b", "sha256"),
    ),
    "register.compared_2b": Action(
        "Purchase register compared with GSTR-2B",
        ("month", "matched", "amount_differs", "docforge_only", "twob_only"),
    ),
    # Invoices by email (V2 P5): who sent it and what became of it, never content or names
    "intake.received": Action(
        "Mail received",
        (
            "sender",
            "status",
            "reason",
            "received",
            "already_received",
            "ignored",
            "refused",
            "held",
        ),
    ),
    "intake.held": Action(
        "Mail held for review",
        (
            "sender",
            "status",
            "reason",
            "received",
            "already_received",
            "ignored",
            "refused",
            "held",
        ),
    ),
    "intake.accepted": Action("Held mail accepted", ("sender", "documents")),
    "intake.discarded": Action("Held mail discarded", ("sender", "attachments")),
    "intake.address_created": Action("Intake address made"),
    "intake.token_revoked": Action("Intake address revoked"),
    "intake.sender_added": Action("Sender listed for intake", ("sender",)),
    "intake.sender_removed": Action("Sender taken off the intake list", ("sender",)),
    "intake.settings_changed": Action("Intake settings changed", ("fields",)),
    # Import-file profiles (V2 P3): the layout's shape, never values from a bill or sample
    "export_profile.created": Action("Import profile made", ("target", "basis", "columns")),
    "export_profile.versioned": Action("Import profile changed", ("version", "basis", "columns")),
    "export_profile.activated": Action("Import profile made active", ("version",)),
    "export_profile.checked": Action(
        "Import profile checked against a file", ("version", "bills", "cells", "equal")
    ),
    # Keys and webhooks
    "api_key.created": Action("API key made", ("name", "role")),
    "api_key.revoked": Action("API key revoked", ("name",)),
    "webhook.created": Action("Webhook made", ("host", "events")),
    "webhook.disabled": Action("Webhook disabled", ("host",)),
    "webhook.enabled": Action("Webhook enabled", ("host",)),
    "webhook.deleted": Action("Webhook deleted", ("host",)),
    "webhook.secret_rotated": Action("Webhook secret rotated", ("secret_version",)),
    "webhook.test_sent": Action("Webhook test sent", ("event_id",)),
    "webhook.delivery_resent": Action(
        "Webhook delivery sent again",
        ("event_id", "event_type", "previous_attempts", "previous_status"),
    ),
    # The log itself
    "audit.exported": Action("Audit log exported", ("rows", "truncated", "filtered")),
}


def label(action: str) -> str:
    known = ACTIONS.get(action)
    return known.label if known else action


def shown(action: str, details: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    """The details `action` may show, and how many others were held back."""
    allowed = ACTIONS[action].shown if action in ACTIONS else ()
    visible = {key: value for key, value in details.items() if key in allowed}
    return visible, len(details) - len(visible)
