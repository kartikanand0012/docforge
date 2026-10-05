"""What a sync run does: compared from what the folder lists and what is held. Pure."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from docforge.sync.drive import DOWNLOADS, EXPORTS, FOLDER, SHORTCUT, VERIFY_PREFIX, DriveFile


@dataclass(frozen=True)
class Held:
    name: str
    md5: str | None
    version: str | None
    state: str  # active, skipped, failed, removed


@dataclass
class SyncPlan:
    new: list[DriveFile] = field(default_factory=list)
    changed: list[DriveFile] = field(default_factory=list)
    renamed: list[DriveFile] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (id, reason)


def _skip_reason(f: DriveFile) -> str | None:
    if f.mime_type == SHORTCUT:
        return "a shortcut: not followed, it may lead outside the folder"
    if f.mime_type not in DOWNLOADS and f.mime_type not in EXPORTS:
        return f"{f.mime_type}: not a file type DocForge reads"
    return None


def _changed(f: DriveFile, h: Held) -> bool:
    # Google's own files are exported afresh each time, with different bytes: only a new
    # version is a change. Other files are compared by their bytes' checksum.
    if f.mime_type in EXPORTS or f.md5 is None:
        return f.version != h.version
    return f.md5 != h.md5


def plan_sync(listing: Sequence[DriveFile], held: Mapping[str, Held]) -> SyncPlan:
    plan = SyncPlan()
    seen: set[str] = set()
    for f in listing:
        if f.mime_type == FOLDER or f.name.startswith(VERIFY_PREFIX):
            continue  # folders are walked; the verification file is not content
        seen.add(f.id)
        reason = _skip_reason(f)
        if reason is not None:
            plan.skipped.append((f.id, reason))
            continue
        h = held.get(f.id)
        if h is None or h.state in ("removed", "skipped", "failed"):
            plan.new.append(f)
        elif _changed(f, h):
            plan.changed.append(f)
        elif f.name != h.name:
            plan.renamed.append(f)
        else:
            plan.unchanged.append(f.id)
    plan.gone = [fid for fid, h in held.items() if fid not in seen and h.state != "removed"]
    return plan
