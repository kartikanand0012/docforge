"""Turning an upload into a PDF, so everything after (parsing, page images, cited boxes)
works on one kind of file.

- A PDF is used as it is.
- An image becomes one page per frame, scaled so the page is no larger than A4; an image
  with more pixels than the limit is refused before it is decoded.
- An office file is converted by LibreOffice, in its own process, with a fresh profile,
  no credentials in its environment and a time limit. LibreOffice does not run macros when
  converting from the command line.
"""

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import warnings
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageSequence

from docforge.formats import IMAGES, OFFICE, Format, extension
from docforge.parsing.raster import images_to_pdf

_A4_LONG_INCHES = 11.69
_MIN_DPI = 72
_IMAGE_QUALITY = 85
_MAX_IMAGE_PIXELS = 60_000_000  # as for rendered pages
_MAX_FRAMES = 100
_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "DATABASE_URL")


class ConversionError(Exception):
    """The file could not be turned into a PDF. The message is for the person who uploaded it."""


class Converter(Protocol):
    def to_pdf(self, data: bytes, fmt: Format) -> bytes:
        """Raises `ConversionError`."""
        ...


class FileConverter:
    MAC_SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"

    def __init__(
        self,
        *,
        soffice: str | None = None,
        timeout_seconds: float = 120.0,
        max_image_pixels: int = _MAX_IMAGE_PIXELS,
    ) -> None:
        self._soffice = soffice
        self._timeout = timeout_seconds
        self._max_pixels = max_image_pixels

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
            return self._office(data, fmt)
        raise ConversionError(f"{fmt} files cannot be converted")

    # --- images ----------------------------------------------------------------------------

    def _image(self, data: bytes) -> bytes:
        pages: list[Image.Image] = []
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size  # from the header; nothing decoded yet
                    if width * height > self._max_pixels:
                        raise ConversionError(
                            f"The image is too large ({width} x {height} pixels)."
                        )
                    dpi = self._dpi(image)
                    for n, frame in enumerate(ImageSequence.Iterator(image)):
                        if n >= _MAX_FRAMES:
                            raise ConversionError(f"The image has more than {_MAX_FRAMES} pages.")
                        pages.append(frame.convert("RGB"))
        except ConversionError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise ConversionError("The image is too large.") from error
        except Exception as error:  # Pillow raises many kinds on a damaged file
            raise ConversionError("The image could not be read.") from error
        return images_to_pdf(pages, dpi, _IMAGE_QUALITY)

    @staticmethod
    def _dpi(image: Image.Image) -> int:
        """Its own resolution, raised as needed so the page's long side fits A4."""
        declared = image.info.get("dpi", (0, 0))[0] or 0
        fit = max(image.size) / _A4_LONG_INCHES
        return max(_MIN_DPI, round(max(float(declared), fit)))

    # --- office files ----------------------------------------------------------------------

    @staticmethod
    def child_environment(home: Path) -> dict[str, str]:
        env = {
            name: value
            for name, value in os.environ.items()
            if not any(marker in name.upper() for marker in _SECRET_MARKERS)
        }
        env["HOME"] = str(home)
        return env

    def _office(self, data: bytes, fmt: Format) -> bytes:
        soffice = self._soffice or self.find_soffice()
        if soffice is None or not Path(soffice).is_file():
            raise ConversionError("LibreOffice is not installed, so office files cannot be read.")
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
                subprocess.run(  # noqa: S603 - fixed arguments, no shell
                    command,
                    env=self.child_environment(work),
                    cwd=work,
                    capture_output=True,
                    timeout=self._timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise ConversionError("Converting the file took too long.") from error
            except OSError as error:
                raise ConversionError("LibreOffice could not be started.") from error
            result = out / "upload.pdf"
            if not result.is_file() or result.stat().st_size == 0:
                raise ConversionError("LibreOffice could not read the file.")
            return result.read_bytes()


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
