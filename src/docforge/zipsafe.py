"""Reading a ZIP of documents safely (V2 P6 bulk upload).

Nothing is ever written to disk from a ZIP: entries are read into memory one at a time, and
an entry's name is only ever reduced to a base name to show (`safe_filename`), never used as a
path. So path traversal cannot happen by construction; a ZIP naming an entry with a path
outside itself (`../x`, `/etc/x`, `C:\\x`) or holding a link is still refused whole, because
it was made to attack, and no document is made from any of it.

Every check is made before the first document is created (`open_zip`), so a refused ZIP
leaves nothing behind:

- at most 200 documents (`MAX_DOCUMENTS`), and 1,000 entries of any kind;
- at most 500 MB unpacked in all (`MAX_TOTAL_BYTES`), by the sizes the ZIP declares and again
  by the bytes actually unpacked, read in bounded chunks: a ZIP whose entries unpack to more
  than they declare is refused (a zip bomb lies about its sizes);
- an entry that unpacks to more than 200 times its packed size, above 1 MB, is a bomb.

What is wrong with one entry only refuses that entry, in words, and the rest go on: a ZIP
inside the ZIP (not opened), an entry protected by a password, one over the per-file limit,
one damaged. Folders and the files operating systems add (`__MACOSX/`, `.DS_Store`,
`Thumbs.db`, `desktop.ini`) are skipped.
"""

import stat
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import IO

from docforge.api.uploads import safe_filename
from docforge.upload_checks import too_large_message

MAX_DOCUMENTS = 200
MAX_ENTRIES = 1000
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_RATIO = 200
_RATIO_FLOOR = 1024 * 1024  # small entries compress well for innocent reasons
_CHUNK = 1024 * 1024
_SYSTEM = {".ds_store", "thumbs.db", "desktop.ini"}

NESTED = (
    "A ZIP inside the ZIP is not opened. Unzip it and upload its files, or upload it as a ZIP "
    "of its own."
)
ENCRYPTED = "This file is protected by a password inside the ZIP. Zip it without a password."
DAMAGED = "This file could not be unpacked from the ZIP; the ZIP may be damaged."
NOT_A_ZIP = "The file could not be read as a ZIP."
BOMB = "The ZIP looks unsafe (it unpacks to far more than its size), so it was not opened."


class ZipRefused(Exception):
    """The whole ZIP refused, before anything was made from it; `message` says why."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Entry:
    position: int  # among the documents, from 0
    filename: str  # its base name, to show
    size: int  # as unpacked
    problem: str | None  # why this entry alone is refused, in words; None to read it
    info: zipfile.ZipInfo


def _unsafe(name: str) -> bool:
    if "\x00" in name or not name:
        return True
    for path in (PurePosixPath(name), PureWindowsPath(name)):
        if path.is_absolute() or path.anchor or ".." in path.parts:
            return True
    return False


def _skipped(info: zipfile.ZipInfo) -> bool:
    parts = PurePosixPath(info.filename.replace("\\", "/")).parts
    base = parts[-1].lower() if parts else ""
    return (
        info.is_dir()
        or "__MACOSX" in parts
        or base in _SYSTEM
        or base.startswith("._")  # a Mac's resource fork beside each file
    )


def _is_link(info: zipfile.ZipInfo) -> bool:
    return info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16)


class SafeZip:
    """A ZIP whose every entry was checked; read each document with `read`."""

    def __init__(self, archive: zipfile.ZipFile, entries: list[Entry]) -> None:
        self._archive = archive
        self.entries = entries

    def read(self, entry: Entry) -> bytes:
        """The entry's bytes, never more than it declared. Raises `ZipRefused` if it unpacks
        to anything else (it was checked to unpack to exactly its size in `open_zip`)."""
        with self._archive.open(entry.info) as handle:
            data = handle.read(entry.size + 1)
        if len(data) != entry.size:
            raise ZipRefused("The ZIP changed while it was read.")
        return data

    def close(self) -> None:
        self._archive.close()


def _unpacked(archive: zipfile.ZipFile, info: zipfile.ZipInfo, allowance: int) -> int:
    """Bytes the entry really unpacks to, read in chunks and never past `allowance` + 1."""
    total = 0
    with archive.open(info) as handle:
        while True:
            chunk = handle.read(min(_CHUNK, allowance - total + 1))
            if not chunk:
                return total
            total += len(chunk)
            if total > allowance:
                return total


def open_zip(source: IO[bytes], *, max_file_bytes: int) -> SafeZip:
    """Check a ZIP whole. Raises `ZipRefused` with the reason in words."""
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as error:
        raise ZipRefused(NOT_A_ZIP) from error
    try:
        return _checked(archive, max_file_bytes)
    except BaseException:
        archive.close()
        raise


def _checked(archive: zipfile.ZipFile, max_file_bytes: int) -> SafeZip:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ZipRefused(f"The ZIP holds {len(infos)} entries; at most {MAX_ENTRIES} are opened.")
    for info in infos:
        if _unsafe(info.filename) or _is_link(info):
            shown = safe_filename(info.filename) or "a file"
            raise ZipRefused(
                f"The ZIP names {shown} with a path outside the ZIP, or as a link, so it was "
                "not opened. Zip the files again from their folder."
            )
    documents = [info for info in infos if not _skipped(info)]
    if not documents:
        raise ZipRefused("The ZIP holds no files.")
    if len(documents) > MAX_DOCUMENTS:
        raise ZipRefused(
            f"The ZIP holds {len(documents)} files; one ZIP may hold up to {MAX_DOCUMENTS}. "
            "Split it into smaller ZIPs."
        )
    declared = sum(info.file_size for info in documents)
    if declared > MAX_TOTAL_BYTES:
        raise ZipRefused(
            f"The ZIP unpacks to {declared // (1024 * 1024)} MB; one ZIP may unpack to at most "
            f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB."
        )
    for info in documents:
        if info.file_size > _RATIO_FLOOR and info.file_size > MAX_RATIO * max(
            info.compress_size, 1
        ):
            raise ZipRefused(BOMB)

    entries: list[Entry] = []
    unpacked_total = 0
    for position, info in enumerate(documents):
        name = safe_filename(info.filename) or f"file-{position + 1}"
        problem: str | None = None
        if info.flag_bits & 0x1:
            problem = ENCRYPTED
        elif info.file_size > max_file_bytes:
            problem = too_large_message(max_file_bytes)
        elif name.lower().endswith(".zip"):
            problem = NESTED
        else:
            try:
                actual = _unpacked(archive, info, info.file_size)
            except zipfile.BadZipFile as error:
                if "overlap" in str(error).lower():
                    raise ZipRefused(BOMB) from error
                problem = DAMAGED
            except (zlib.error, OSError, EOFError, NotImplementedError, RuntimeError):
                problem = DAMAGED
            else:
                if actual > info.file_size:
                    raise ZipRefused(BOMB)
                if actual != info.file_size:
                    problem = DAMAGED
                unpacked_total += actual
                if unpacked_total > MAX_TOTAL_BYTES:
                    raise ZipRefused(BOMB)
        entries.append(Entry(position, name, info.file_size, problem, info))
    return SafeZip(archive, entries)
