"""Erasing stored files once their rows are gone: a deleted document's, or a whole workspace's.

The database decides what is to be erased, in the transaction that deletes (or hides) the
rows: `docforge_delete_document` and `docforge_delete_account` (migrations 0033, 0037) each
record an erasure and return its id and a token; only that token (or the owner) clears it
once the files are gone. The files are removed after that transaction commits, best effort:
a store that is down leaves the erasure recorded, and `python -m docforge.erasure` (run by
the `admin` role on each release, as the owner) finishes every erasure still recorded.

Originals are content-addressed: a document deleted and the same bytes uploaded again share
one object. A document's files are therefore erased only while no live document of its
workspace has the same content, checked under the lock an upload of those bytes takes.
"""

import logging
import re
import sys
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import exists, func, select, text
from sqlalchemy.orm import Session

from docforge.db.models import Document
from docforge.db.session import SessionFactory
from docforge.db.tenancy import tenant_scope
from docforge.storage import ObjectStore

logger = logging.getLogger(__name__)

ANCHORS_PREFIX = "audit-anchors"


@dataclass(frozen=True)
class Erasure:
    id: int
    kind: Literal["document", "workspace"]
    tenant_id: uuid.UUID
    sha256: str | None = None
    storage_key: str | None = None
    # Given with the erasure when it was recorded: only its holder may say it is done.
    token: uuid.UUID | None = None


_SHA256 = re.compile(r"[0-9a-f]{64}")
_ORIGINAL_EXTENSION = re.compile(r"\.[a-z0-9]{1,8}")
TENANT_PREFIXES = ("originals", "renditions")


def _original_key(erasure: Erasure) -> str:
    """The document's original's key, checked to be one: `originals/<tenant>/<sha256>.<ext>`
    and nothing else, so a recorded erasure can never delete another workspace's file."""
    key, sha = erasure.storage_key or "", erasure.sha256 or ""
    stem = f"originals/{erasure.tenant_id}/{sha}"
    if (
        not _SHA256.fullmatch(sha)
        or not key.startswith(stem)
        or not _ORIGINAL_EXTENSION.fullmatch(key[len(stem) :])
    ):
        raise ValueError(f"{key!r} is not the original of this document")
    return key


def lock_content(session: Session, tenant_id: uuid.UUID, sha256: str) -> None:
    """Until this transaction ends, no one else uploads or erases these bytes here."""
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"content:{tenant_id}:{sha256}")))
    )


def live_copy(session: Session, tenant_id: uuid.UUID, sha256: str) -> bool:
    """Whether a document of the workspace that is not deleted holds these bytes."""
    return bool(
        session.scalar(
            select(
                exists().where(
                    Document.tenant_id == tenant_id,
                    Document.sha256 == sha256,
                    Document.deleted_at.is_(None),
                )
            )
        )
    )


class Eraser:
    def __init__(
        self, sessions: SessionFactory, store: ObjectStore, anchors_prefix: str = ANCHORS_PREFIX
    ) -> None:
        self._sessions = sessions
        self._store = store
        self._anchors_prefix = anchors_prefix

    def erase(self, erasure: Erasure) -> None:
        """Remove the files and then the record of the erasure. Raises what the store raises;
        the erasure then stays recorded, to be finished later."""
        if erasure.kind == "workspace":
            tenant = erasure.tenant_id
            for prefix in (*TENANT_PREFIXES, self._anchors_prefix):
                self._store.delete_prefix(f"{prefix}/{tenant}/")
            with self._sessions.begin() as session:
                self._done(session, erasure)
            return
        original = _original_key(erasure)
        sha = str(erasure.sha256)
        with tenant_scope(erasure.tenant_id), self._sessions.begin() as session:
            lock_content(session, erasure.tenant_id, sha)
            if not live_copy(session, erasure.tenant_id, sha):
                self._store.delete(original)
                # Every PDF made from it, whichever converter made it: the older single
                # rendition, and the folder of one per converter version.
                self._store.delete(f"renditions/{erasure.tenant_id}/{sha}.pdf")
                self._store.delete_prefix(f"renditions/{erasure.tenant_id}/{sha}/")
            self._done(session, erasure)

    @staticmethod
    def _done(session: Session, erasure: Erasure) -> None:
        if erasure.token is not None:  # an erasure recorded in a test has none to clear
            session.execute(select(func.docforge_erasure_done(erasure.id, erasure.token)))

    def erase_quietly(self, erasure: Erasure) -> bool:
        """`erase`, after a commit that must stand whatever the store does: a failure is
        logged (it stays recorded, for `python -m docforge.erasure`). Whether it was done."""
        try:
            self.erase(erasure)
        except Exception:
            logger.warning(
                "stored files of %s erasure %s (tenant %s) are not erased yet; "
                "python -m docforge.erasure finishes it",
                erasure.kind, erasure.id, erasure.tenant_id, exc_info=True,
            )  # fmt: skip
            return False
        return True


def pending(session: Session) -> list[Erasure]:
    """Every erasure still recorded, oldest first (as the owner: the application cannot)."""
    rows = session.execute(
        text("SELECT id, kind, tenant_id, sha256, storage_key, token FROM erasures ORDER BY id")
    ).all()
    return [Erasure(r.id, r.kind, r.tenant_id, r.sha256, r.storage_key, r.token) for r in rows]


def finish_all(
    sessions: SessionFactory, store: ObjectStore, anchors_prefix: str = ANCHORS_PREFIX
) -> tuple[int, int]:
    """Finish every recorded erasure; (done, still left)."""
    with sessions() as session:
        found = pending(session)
    eraser = Eraser(sessions, store, anchors_prefix)
    done = sum(eraser.erase_quietly(erasure) for erasure in found)
    return done, len(found) - done


def sweep_orphans(
    sessions: SessionFactory, store: ObjectStore, anchors_prefix: str = ANCHORS_PREFIX
) -> int:
    """As the owner: remove every file kept under a workspace that no longer exists (left by
    work that was under way when it was deleted). Returns how many were removed. Only
    folders named by a workspace id are looked at."""
    removed = 0
    for top in (*TENANT_PREFIXES, anchors_prefix):
        named = set()
        for child in store.children(f"{top}/"):
            try:
                named.add(uuid.UUID(child))
            except ValueError:
                continue
        if not named:
            continue
        with sessions() as session:
            present = set(
                session.scalars(
                    text("SELECT id FROM tenants WHERE id = ANY(:ids)").bindparams(ids=list(named))
                )
            )
        for tenant in named - present:
            removed += store.delete_prefix(f"{top}/{tenant}/")
    return removed


def main() -> int:
    from docforge.config import get_settings
    from docforge.db.session import make_engine, make_session_factory
    from docforge.storage import S3ObjectStore

    settings = get_settings()
    # As the owner: only it reads the record of erasures still to do.
    engine = make_engine(settings.migration_database_url.get_secret_value())
    try:
        sessions, store = make_session_factory(engine), S3ObjectStore.from_settings(settings)
        done, left = finish_all(sessions, store)
        swept = sweep_orphans(sessions, store)
    finally:
        engine.dispose()
    print(f"Erased the stored files of {done} deleted documents or workspaces; {left} left")
    print(f"Removed {swept} files left under workspaces that no longer exist")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    raise SystemExit(main())
