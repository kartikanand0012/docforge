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
    "match.created": Action("Matched", ("counterpart_document_id", "decision", "discrepancies")),
    "processing.retry_scheduled": Action(
        "Reading to be retried", ("version_no", "attempt", "error")
    ),
    "processing.failed": Action("Reading failed", ("version_no", "error")),
    "indexing.completed": Action("Ready to search and ask", ("version_no",)),
    # People
    "reviewer.pin_failed": Action("Wrong PIN", ("attempt", "at")),
    "review.corrected": Action("Value corrected", ("path", "version_no", "changed")),
    "review.signed": Action(
        "Review signed", ("outcome", "version_no", "record_sha256", "overridden")
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
