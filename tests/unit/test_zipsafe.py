"""The ZIP reader: every check made before anything is made from a ZIP (AC-P6-2)."""

import io
import zipfile

import pytest

from docforge import zipsafe
from docforge.zipsafe import BOMB, DAMAGED, ENCRYPTED, NESTED, ZipRefused, open_zip

LIMIT = 10 * 1024 * 1024
PDF = b"%PDF-1.4\n" + b"x" * 200 + b"\n%%EOF\n"


def make(entries: dict[str, bytes], method: int = zipfile.ZIP_DEFLATED) -> io.BytesIO:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", method) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    out.seek(0)
    return out


def test_documents_are_read_by_base_name_and_system_files_skipped() -> None:
    source = make({
        "bills/oct/a.pdf": PDF,
        "b.pdf": PDF + b"2",
        "__MACOSX/bills/._a.pdf": b"junk",
        ".DS_Store": b"junk",
        "bills/": b"",
        "Thumbs.db": b"junk",
    })  # fmt: skip

    safe = open_zip(source, max_file_bytes=LIMIT)

    assert [(e.filename, e.problem) for e in safe.entries] == [("a.pdf", None), ("b.pdf", None)]
    assert safe.read(safe.entries[0]) == PDF


@pytest.mark.parametrize(
    "name", ["../../etc/passwd", "/etc/passwd", "a/../../x.pdf", "C:\\Windows\\x.pdf",
             "..\\..\\x.pdf", "\\\\server\\share\\x.pdf"],
)  # fmt: skip
def test_a_name_reaching_outside_the_zip_refuses_it_whole(name: str) -> None:
    source = make({"good.pdf": PDF, name: PDF})

    with pytest.raises(ZipRefused, match="path outside the ZIP"):
        open_zip(source, max_file_bytes=LIMIT)


def test_a_link_refuses_the_zip_whole() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        link = zipfile.ZipInfo("invoice.pdf")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "/etc/passwd")
    out.seek(0)

    with pytest.raises(ZipRefused, match="as a link"):
        open_zip(out, max_file_bytes=LIMIT)


def test_a_highly_compressed_entry_is_a_bomb() -> None:
    source = make({"bomb.pdf": b"\0" * (5 * 1024 * 1024)})  # ~5 KB packed, 5 MB unpacked

    with pytest.raises(ZipRefused) as refused:
        open_zip(source, max_file_bytes=LIMIT)
    assert refused.value.message == BOMB


def test_an_entry_declaring_less_than_it_holds_is_never_read_past_its_size() -> None:
    data = PDF * 50
    source = make({"liar.pdf": data, "ok.pdf": PDF})
    # Rewrite liar's declared size (local header and central directory) to something smaller.
    raw = source.getvalue().replace(len(data).to_bytes(4, "little"), (100).to_bytes(4, "little"))

    safe = open_zip(io.BytesIO(raw), max_file_bytes=LIMIT)

    # Unpacking stops at the declared 100 bytes; the check then fails, and only it is refused.
    assert [(e.filename, e.size, e.problem) for e in safe.entries] == [
        ("liar.pdf", 100, DAMAGED), ("ok.pdf", len(PDF), None),
    ]  # fmt: skip


def test_more_than_the_total_declared_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zipsafe, "MAX_TOTAL_BYTES", 1000)
    source = make({"a.pdf": PDF * 3, "b.pdf": PDF * 3})

    with pytest.raises(ZipRefused, match="unpacks to"):
        open_zip(source, max_file_bytes=LIMIT)


def test_more_than_200_documents_are_refused() -> None:
    source = make({f"{n}.pdf": PDF + str(n).encode() for n in range(201)})

    with pytest.raises(ZipRefused, match="201 files; one ZIP may hold up to 200"):
        open_zip(source, max_file_bytes=LIMIT)


def test_two_hundred_documents_are_taken() -> None:
    source = make({f"{n}.pdf": PDF + str(n).encode() for n in range(200)})

    assert len(open_zip(source, max_file_bytes=LIMIT).entries) == 200


def test_one_bad_entry_is_refused_alone_in_words() -> None:
    source = make({
        "inner.zip": make({"x.pdf": PDF}).getvalue(),
        "big.pdf": PDF + b"y" * 2000,
        "ok.pdf": PDF,
    }, method=zipfile.ZIP_STORED)  # fmt: skip

    safe = open_zip(source, max_file_bytes=1000)

    problems = {e.filename: e.problem for e in safe.entries}
    assert problems["inner.zip"] == NESTED
    assert problems["big.pdf"] == "The file is larger than the limit of 1000 bytes."
    assert problems["ok.pdf"] is None


def test_an_encrypted_entry_is_refused_alone() -> None:
    source = make({"locked.pdf": PDF, "ok.pdf": PDF + b"1"}, method=zipfile.ZIP_STORED)
    raw = bytearray(source.getvalue())
    # Set the "encrypted" flag on the first entry's local header and central record.
    raw[6] |= 0x1
    central = bytes(raw).find(b"PK\x01\x02")
    raw[central + 8] |= 0x1

    safe = open_zip(io.BytesIO(bytes(raw)), max_file_bytes=LIMIT)

    assert [e.problem for e in safe.entries] == [ENCRYPTED, None]


def test_a_damaged_entry_is_refused_alone() -> None:
    source = make({"a.pdf": PDF * 20, "b.pdf": PDF + b"b"})
    raw = bytearray(source.getvalue())
    raw[60] ^= 0xFF  # inside a.pdf's compressed bytes

    safe = open_zip(io.BytesIO(bytes(raw)), max_file_bytes=LIMIT)

    assert [e.problem for e in safe.entries] == [DAMAGED, None]


def test_not_a_zip_is_refused() -> None:
    with pytest.raises(ZipRefused, match="could not be read as a ZIP"):
        open_zip(io.BytesIO(PDF), max_file_bytes=LIMIT)


def test_an_empty_zip_is_refused() -> None:
    with pytest.raises(ZipRefused, match="holds no files"):
        open_zip(make({"folder/": b""}), max_file_bytes=LIMIT)
