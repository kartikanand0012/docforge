"""Review claims: while one reviewer has a document open, nobody else may change it.

A claim is a lease of `CLAIM_FOR`, renewed by the reviewer's screen while it is open, so a
closed tab or a lost connection frees the document within minutes without anyone releasing
it. Claims are optional: no claim, or one that has run out, never blocks anything, so a
caller that does not claim (an older client, an integration) works as before.

Checked inside the transaction that holds the document's row lock, as correcting, signing
and reading the document again all do, so a claim and a change cannot cross.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from docforge.db.models import ReviewClaim, Reviewer

CLAIM_FOR = timedelta(minutes=5)


class ClaimedByOther(Exception):
    """Another reviewer has the document open; their claim has not run out. Its message is
    the one shown to the person refused."""

    def __init__(self, name: str, since: datetime) -> None:
        super().__init__(
            f"{name} is reviewing this document. Wait until they finish, "
            "or ask an administrator to take over."
        )
        self.name = name
        self.since = since


class TakeOverRefused(Exception):
    """Only an administrator may take a review over from someone else."""


def live_claim(
    session: Session, document_id: uuid.UUID, now: datetime
) -> tuple[ReviewClaim, str] | None:
    """The document's claim and its holder's name, unless there is none or it has run out."""
    row = session.execute(
        select(ReviewClaim, Reviewer.name)
        .join(Reviewer, Reviewer.id == ReviewClaim.reviewer_id)
        .where(ReviewClaim.document_id == document_id, ReviewClaim.expires_at > now)
    ).first()
    return (row[0], row[1]) if row is not None else None


def refuse_if_claimed(
    session: Session, document_id: uuid.UUID, reviewer_id: uuid.UUID | None, now: datetime
) -> None:
    """Raise `ClaimedByOther` when someone other than `reviewer_id` holds a live claim. With
    no reviewer (an API key), anyone's live claim refuses."""
    found = live_claim(session, document_id, now)
    if found is not None and found[0].reviewer_id != reviewer_id:
        raise ClaimedByOther(found[1], found[0].claimed_at)
