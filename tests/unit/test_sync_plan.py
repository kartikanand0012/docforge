"""Deciding what a sync run does, from what the folder lists and what is held: pure, so
every case is tested without Drive or a database."""

from docforge.sync.drive import FOLDER, GOOGLE_DOC, SHORTCUT, DriveFile
from docforge.sync.plan import Held, plan_sync

PDF = "application/pdf"


def file(
    fid: str, name: str = "a.pdf", mime: str = PDF, md5: str | None = "m1", version: str = "1"
) -> DriveFile:
    return DriveFile(id=fid, name=name, mime_type=mime, md5=md5, version=version, size=100)


def held(
    name: str = "a.pdf", md5: str | None = "m1", version: str = "1", state: str = "active"
) -> Held:
    return Held(name=name, md5=md5, version=version, state=state)


def test_a_file_not_held_is_new() -> None:
    plan = plan_sync([file("f1")], {})
    assert [f.id for f in plan.new] == ["f1"]


def test_a_file_whose_bytes_changed_is_changed() -> None:
    plan = plan_sync([file("f1", md5="m2", version="2")], {"f1": held()})
    assert [f.id for f in plan.changed] == ["f1"]


def test_a_google_doc_is_changed_only_by_its_version() -> None:
    """Exports differ byte to byte each time; only a new version is a change."""
    doc = file("d1", "notes", GOOGLE_DOC, md5=None, version="7")
    assert plan_sync([doc], {"d1": held("notes", md5=None, version="7")}).unchanged == ["d1"]
    assert [
        f.id for f in plan_sync([doc], {"d1": held("notes", md5=None, version="6")}).changed
    ] == ["d1"]


def test_a_renamed_file_with_the_same_bytes_is_only_renamed() -> None:
    plan = plan_sync([file("f1", name="b.pdf", version="2")], {"f1": held()})
    assert [f.id for f in plan.renamed] == ["f1"] and plan.changed == []


def test_a_held_file_no_longer_listed_is_gone() -> None:
    plan = plan_sync([], {"f1": held(), "f2": held(state="removed"), "f3": held(state="skipped")})
    assert plan.gone == ["f1", "f3"]


def test_a_file_removed_earlier_and_back_again_is_new() -> None:
    plan = plan_sync([file("f1")], {"f1": held(state="removed")})
    assert [f.id for f in plan.new] == ["f1"]


def test_shortcuts_folders_and_unsupported_types_are_skipped_with_a_reason() -> None:
    listing = [
        file("s1", "link", SHORTCUT, md5=None),
        file("z1", "archive.zip", "application/zip"),
        file("v1", "clip.mp4", "video/mp4"),
        file("d0", "sub", FOLDER, md5=None),
    ]
    plan = plan_sync(listing, {})
    reasons = dict(plan.skipped)
    assert set(reasons) == {"s1", "z1", "v1"}
    assert "shortcut" in reasons["s1"] and "not a file type" in reasons["z1"]
    assert plan.new == []


def test_the_verification_file_is_never_planned() -> None:
    listing = [file("t1", "docforge-verify-abc", "text/plain"), file("f1")]
    plan = plan_sync(listing, {})
    assert [f.id for f in plan.new] == ["f1"] and plan.skipped == []


def test_a_skipped_file_that_becomes_supported_is_new() -> None:
    plan = plan_sync([file("f1")], {"f1": held(state="skipped")})
    assert [f.id for f in plan.new] == ["f1"]
