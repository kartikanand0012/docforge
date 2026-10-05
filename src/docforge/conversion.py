"""Turning an upload into a PDF, so everything after (parsing, page images, cited boxes)
works on one kind of file.

- A PDF is used as it is.
- An image becomes one page per frame, scaled so the page is no larger than A4, transparent
  parts on white. Every frame's pixels, and all of them together, are held to a limit before
  the frame is decoded.
- An office file is checked first: one that links to anything outside itself (other than a
  hyperlink), carries macros, or unpacks to more than the limit is refused. LibreOffice then
  converts it with a fresh profile, only the environment it needs, and a time limit; every
  process it started is killed with it.

In production `IsolatedConverter` runs all of this in a child process with its own time and
memory limits, as the parser does, so a file that makes a decoder or LibreOffice run away
costs that one document and never the worker.
"""

import contextlib
import hashlib
import io
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import warnings
import zipfile
from collections.abc import Callable, Iterator
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from pathlib import Path
from typing import Protocol

import psutil
from PIL import Image, ImageSequence

from docforge.formats import IMAGES, OFFICE, Format, extension
from docforge.parsing.raster import images_to_pdf

_A4_LONG_INCHES = 11.69
_MIN_DPI = 72
_IMAGE_QUALITY = 85
_MAX_IMAGE_PIXELS = 60_000_000  # all frames together; an A0 page at 200 dpi
_MAX_FRAMES = 100
_MAX_UNPACKED_BYTES = 200 * 1024 * 1024
_MAX_ENTRIES = 10_000
_MAX_RELS_BYTES = 1024 * 1024
_MAX_OUTPUT_BYTES = 50 * 1024 * 1024
_GIB = 1024**3
_POLL_SECONDS = 0.2
_ENVIRONMENT = ("PATH", "LANG", "LC_ALL")  # all LibreOffice is given from the worker's
_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "DATABASE_URL", "DSN", "AWS")
_RELATIONSHIP = re.compile(rb"<Relationship\b[^>]*>", re.IGNORECASE)
_EXTERNAL = re.compile(rb"""TargetMode\s*=\s*["']External["']""", re.IGNORECASE)
_HYPERLINK = re.compile(rb"""Type\s*=\s*["'][^"']*/hyperlink["']""", re.IGNORECASE)


class ConversionError(Exception):
    """The file could not be turned into a PDF. The message is for the person who uploaded it."""


class ConverterUnavailable(Exception):
    """The worker cannot convert files now (LibreOffice missing or not starting). Not the
    file's fault: the job is retried."""


class Converter(Protocol):
    def to_pdf(self, data: bytes, fmt: Format) -> bytes:
        """Raises `ConversionError` or `ConverterUnavailable`."""
        ...


class FileConverter:
    MAC_SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"

    def __init__(
        self,
        *,
        soffice: str | None = None,
        timeout_seconds: float = 120.0,
        max_image_pixels: int = _MAX_IMAGE_PIXELS,
        max_unpacked_bytes: int = _MAX_UNPACKED_BYTES,
    ) -> None:
        self._soffice = soffice
        self._timeout = timeout_seconds
        self._max_pixels = max_image_pixels
        self._max_unpacked = max_unpacked_bytes

    @classmethod
    def find_soffice(cls) -> str | None:
        found = shutil.which("soffice") or shutil.which("libreoffice")
        if found:
            return found
        return cls.MAC_SOFFICE if Path(cls.MAC_SOFFICE).is_file() else None

    def to_pdf(self, data: bytes, fmt: Format) -> bytes:
        if fmt == "pdf":
            return data
        if fmt in IMAGES:
            return self._image(data)
        if fmt in OFFICE:
            self._check_office(data)
            return self._office(data, fmt)
        raise ConversionError(f"{fmt} files cannot be converted.")

    # --- images ----------------------------------------------------------------------------

    def _image(self, data: bytes) -> bytes:
        pages: list[Image.Image] = []
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    declared = image.info.get("dpi", (0, 0))[0] or 0
                    total = 0
                    for n, frame in enumerate(ImageSequence.Iterator(image)):
                        if n >= _MAX_FRAMES:
                            raise ConversionError(f"The image has more than {_MAX_FRAMES} pages.")
                        width, height = frame.size  # from the frame's header; not decoded yet
                        total += width * height
                        if total > self._max_pixels:
                            raise ConversionError(
                                f"The image is too large (more than {self._max_pixels:,} pixels)."
                            )
                        pages.append(_on_white(frame))
        except ConversionError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise ConversionError("The image is too large.") from error
        except Exception as error:  # Pillow raises many kinds on a damaged file
            raise ConversionError("The image could not be read.") from error
        longest = max(max(page.size) for page in pages)
        dpi = max(_MIN_DPI, round(max(float(declared), longest / _A4_LONG_INCHES)))
        return images_to_pdf(pages, dpi, _IMAGE_QUALITY)

    # --- office files ----------------------------------------------------------------------

    def _check_office(self, data: bytes) -> None:
        """Refuse what LibreOffice should never be handed, reading only what is needed."""
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, ValueError, OSError) as error:
            raise ConversionError("The file could not be read.") from error
        with archive:
            items = archive.infolist()
            if len(items) > _MAX_ENTRIES or sum(i.file_size for i in items) > self._max_unpacked:
                raise ConversionError("The file is too large once unpacked.")
            for item in items:
                name = item.filename.lower()
                if name.endswith("vbaproject.bin") or "/activex/" in name:
                    raise ConversionError("The file contains macros, which are not accepted.")
                if name.endswith(".rels"):
                    if item.file_size > _MAX_RELS_BYTES:
                        raise ConversionError("The file could not be read.")
                    for relationship in _RELATIONSHIP.findall(archive.read(item)):
                        if _EXTERNAL.search(relationship) and not _HYPERLINK.search(relationship):
                            raise ConversionError(
                                "The file links to content outside itself (a linked image, "
                                "data source or template), which is not accepted."
                            )

    @staticmethod
    def child_environment(home: Path) -> dict[str, str]:
        """Only what LibreOffice needs: nothing of the worker's settings or credentials."""
        env = {name: os.environ[name] for name in _ENVIRONMENT if name in os.environ}
        env.setdefault("PATH", "/usr/bin:/bin")
        env.update(HOME=str(home), TMPDIR=str(home), SAL_USE_VCLPLUGIN="svp")
        return env

    def _office(self, data: bytes, fmt: Format) -> bytes:
        soffice = self._soffice or self.find_soffice()
        if soffice is None or not Path(soffice).is_file():
            raise ConverterUnavailable("LibreOffice is not installed on this worker.")
        with tempfile.TemporaryDirectory(prefix="docforge-convert-") as tmp:
            work = Path(tmp)
            source = work / f"upload.{extension(fmt)}"
            source.write_bytes(data)
            out = work / "out"
            command = [
                soffice,
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--nodefault",
                f"-env:UserInstallation={(work / 'profile').as_uri()}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(out),
                str(source),
            ]
            try:
                # A session of its own, so every process LibreOffice starts can be killed.
                process = subprocess.Popen(  # noqa: S603 - fixed arguments, no shell
                    command,
                    env=self.child_environment(work),
                    cwd=work,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as error:
                raise ConverterUnavailable("LibreOffice could not be started.") from error
            try:
                process.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired as error:
                raise ConversionError("Converting the file took too long.") from error
            finally:
                _kill_group(process.pid)
                process.wait()
            result = out / "upload.pdf"
            if not result.is_file() or result.stat().st_size == 0:
                raise ConversionError("LibreOffice could not read the file.")
            return result.read_bytes()


def _on_white(frame: Image.Image) -> Image.Image:
    """The frame as RGB, any transparent part made white rather than black."""
    if frame.mode in ("RGBA", "LA") or (frame.mode == "P" and "transparency" in frame.info):
        rgba = frame.convert("RGBA")
        page = Image.new("RGB", rgba.size, "white")
        page.paste(rgba, mask=rgba.getchannel("A"))
        return page
    return frame.convert("RGB")


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _tree(pid: int) -> Iterator[psutil.Process]:
    with contextlib.suppress(psutil.Error):
        root = psutil.Process(pid)
        yield root
        yield from root.children(recursive=True)


# --- in a child process ------------------------------------------------------------------


def _convert_in_child(
    factory: Callable[[], Converter],
    connection: Connection,
    data: bytes,
    fmt: Format,
    max_output: int,
) -> None:
    os.setsid()  # its own process group: killing it kills whatever it started
    for name in list(os.environ):
        if any(marker in name.upper() for marker in _SECRET_MARKERS):
            del os.environ[name]
    try:
        pdf = factory().to_pdf(data, fmt)
        too_large = b"CThe PDF made from the file is too large."
        reply = too_large if len(pdf) > max_output else b"P" + pdf
    except ConversionError as error:
        reply = b"C" + str(error).encode()
    except ConverterUnavailable as error:
        reply = b"U" + str(error).encode()
    except Exception as error:
        reply = b"C" + f"The file could not be converted ({type(error).__name__}).".encode()
    connection.send_bytes(reply)


class IsolatedConverter:
    """A `Converter` that does each conversion in a fresh child process, with limits.

    `factory` must be importable by name (a class, or a `functools.partial` of one), because the
    child is started fresh rather than forked. The child's whole process tree counts toward the
    memory limit, and the whole tree is killed when the conversion ends, however it ends.
    """

    def __init__(
        self,
        factory: Callable[[], Converter],
        *,
        timeout_seconds: float = 180.0,
        max_rss_bytes: int = 2 * _GIB,
        max_output_bytes: int = _MAX_OUTPUT_BYTES,
    ) -> None:
        self._factory = factory
        self._timeout = timeout_seconds
        self._max_rss = max_rss_bytes
        self._max_output = max_output_bytes

    def to_pdf(self, data: bytes, fmt: Format) -> bytes:
        if fmt == "pdf":
            return data
        context = multiprocessing.get_context("spawn")
        ours, theirs = context.Pipe(duplex=False)
        process: SpawnProcess = context.Process(
            target=_convert_in_child,
            args=(self._factory, theirs, data, fmt, self._max_output),
            daemon=True,
        )
        process.start()
        theirs.close()
        try:
            reply = self._wait(process, ours)
        finally:
            ours.close()
            self._stop(process)
        kind, body = reply[:1], reply[1:]
        if kind == b"U":
            raise ConverterUnavailable(body.decode())
        if kind != b"P":
            raise ConversionError(body.decode())
        if len(body) > self._max_output:
            raise ConversionError("The PDF made from the file is too large.")
        return body

    def _wait(self, process: SpawnProcess, connection: Connection) -> bytes:
        deadline = time.monotonic() + self._timeout
        while not connection.poll(_POLL_SECONDS):
            if not process.is_alive():
                raise ConversionError("The converter stopped unexpectedly.")
            if time.monotonic() > deadline:
                raise ConversionError("Converting the file took too long.")
            if process.pid is not None and self._rss(process.pid) > self._max_rss:
                raise ConversionError(
                    "Converting the file was stopped: it used more than the memory limit of "
                    f"{self._max_rss // 1024**2} MB."
                )
        try:
            return connection.recv_bytes(self._max_output + 1024)
        except (EOFError, OSError) as error:
            raise ConversionError("The converter stopped unexpectedly.") from error

    @staticmethod
    def _rss(pid: int) -> int:
        total = 0
        for member in _tree(pid):
            with contextlib.suppress(psutil.Error):
                total += int(member.memory_info().rss)
        return total

    @staticmethod
    def _stop(process: SpawnProcess) -> None:
        if process.pid is not None:
            members = list(_tree(process.pid))
            _kill_group(process.pid)  # the child is its group's leader (os.setsid)
            for member in members:  # and anything that left the group
                with contextlib.suppress(psutil.Error):
                    member.kill()
        process.join(timeout=5)


class RecordingConverter:
    """Serves recorded conversions, keyed by the file's hash; with `inner`, records misses.

    The browser test and demos replay conversions, so they need no LibreOffice and give the
    same PDF, and so the same recorded parse, on every machine.
    """

    def __init__(self, directory: Path, inner: Converter | None) -> None:
        self._directory = directory
        self._inner = inner

    def to_pdf(self, data: bytes, fmt: Format) -> bytes:
        if fmt == "pdf":
            return data
        path = self._directory / f"{hashlib.sha256(data).hexdigest()[:24]}.pdf"
        if path.is_file():
            return path.read_bytes()
        if self._inner is None:
            raise ConversionError("There is no recorded conversion for this file.")
        pdf = self._inner.to_pdf(data, fmt)
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(pdf)
        os.replace(temporary, path)
        return pdf


__all__ = [
    "ConversionError",
    "Converter",
    "ConverterUnavailable",
    "FileConverter",
    "IsolatedConverter",
    "RecordingConverter",
]
