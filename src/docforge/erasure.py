"""Erasing stored files once their rows are gone: a deleted document's, or a whole workspace's.

The database decides what is to be erased, in the transaction that deletes (or hides) the
rows: `docforge_delete_document` and `docforge_delete_account` (migration 0033) each record
an erasure. The files are removed after that transaction commits, best effort: a store that
is down leaves the erasure recorded, and `python -m docforge.erasure` (run by the `admin`
role on each release, as the owner) finishes every erasure still recorded.

Originals are content-addressed: a document deleted and the same bytes uploaded again share
one object. A document's files are therefore erased only while no live document of its
workspace has the same content, checked under the lock an upload of those bytes takes.
"""

import logging
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
            for prefix in ("originals", "renditions", self._anchors_prefix):
                self._store.delete_prefix(f"{prefix}/{tenant}/")
            with self._sessions.begin() as session:
                session.execute(select(func.docforge_erasure_done(erasure.id)))
            return
        assert erasure.sha256 is not None  # noqa: S101 - a document's erasure always has it
        with tenant_scope(erasure.tenant_id), self._sessions.begin() as session:
            lock_content(session, erasure.tenant_id, erasure.sha256)
            if not live_copy(session, erasure.tenant_id, erasure.sha256):
                if erasure.storage_key:
                    self._store.delete(erasure.storage_key)
                # Every PDF made from it, whichever converter made it.
                self._store.delete_prefix(f"renditions/{erasure.tenant_id}/{erasure.sha256}")
            session.execute(select(func.docforge_erasure_done(erasure.id)))

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
        text("SELECT id, kind, tenant_id, sha256, storage_key FROM erasures ORDER BY id")
    ).all()
    return [Erasure(r.id, r.kind, r.tenant_id, r.sha256, r.storage_key) for r in rows]


def finish_all(
    sessions: SessionFactory, store: ObjectStore, anchors_prefix: str = ANCHORS_PREFIX
) -> tuple[int, int]:
    """Finish every recorded erasure; (done, still left)."""
    with sessions() as session:
        found = pending(session)
    eraser = Eraser(sessions, store, anchors_prefix)
    done = sum(eraser.erase_quietly(erasure) for erasure in found)
    return done, len(found) - done


def main() -> int:
    from docforge.config import get_settings
    from docforge.db.session import make_engine, make_session_factory
    from docforge.storage import S3ObjectStore

    settings = get_settings()
    # As the owner: only it reads the record of erasures still to do.
    engine = make_engine(settings.migration_database_url.get_secret_value())
    try:
        done, left = finish_all(make_session_factory(engine), S3ObjectStore.from_settings(settings))
    finally:
        engine.dispose()
    print(f"Erased the stored files of {done} deleted documents or workspaces; {left} left")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    raise SystemExit(main())
