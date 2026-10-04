"""Converters for the isolation tests, in a module a child process can import."""

import os
import subprocess
import sys
import time
from pathlib import Path

from docforge.conversion import ConversionError
from docforge.formats import Format


class CommandConverter:
    """Does what the bytes say."""

    def to_pdf(self, data: bytes, fmt: Format) -> bytes:
        command = data.decode()
        if command == "sleep":
            time.sleep(60)
        if command.startswith("grandchild:"):
            # A process of its own, as LibreOffice starts soffice.bin; its pid is left in a file.
            pidfile = Path(command.split(":", 1)[1])
            sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
            child = subprocess.Popen(sleeper)  # noqa: S603
            pidfile.write_text(str(child.pid))
            time.sleep(60)
        if command == "hoard":
            hoard = bytearray(400 * 1024 * 1024)
            hoard[::4096] = b"x" * len(hoard[::4096])
            time.sleep(60)
        if command == "exit":
            os._exit(3)
        if command == "refuse":
            raise ConversionError("The file is not one we can convert.")
        if command == "big":
            return b"%PDF-" + b"x" * 2048
        return b"%PDF-converted"
