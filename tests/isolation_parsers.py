"""Parsers for the isolation tests. In a module of their own so a child process can import them."""

import os
import time

from docforge.parsing.base import (
    BBox,
    Block,
    DocumentTooLarge,
    NoTextLayer,
    Page,
    ParsedDocument,
    ParseError,
)


class CommandParser:
    """Does what the "PDF" bytes say, and reports the process it ran in as its version."""

    name = "command"
    version = "0"

    def __init__(self) -> None:
        self.hoard: bytearray | None = None

    def parse(self, pdf: bytes) -> ParsedDocument:
        command = pdf.decode()
        if command == "env":
            command = " ".join(f"{key}={value}" for key, value in sorted(os.environ.items()))
        if command == "exit":
            os._exit(3)
        if command == "sleep":
            time.sleep(60)
        if command == "hoard":
            self.hoard = bytearray(400 * 1024 * 1024)
            self.hoard[::4096] = b"x" * len(self.hoard[::4096])  # touch every page
            time.sleep(60)
        if command == "no-text":
            raise NoTextLayer("nothing to read")
        if command == "too-large":
            raise DocumentTooLarge("too many pages")
        if command == "unreadable":
            raise ParseError("not a PDF")
        if command == "bug":
            raise RuntimeError("unexpected")
        return ParsedDocument(
            parser=self.name,
            parser_version=str(os.getpid()),
            pages=(Page(number=1, width=10, height=10),),
            blocks=(
                Block(
                    id="b1", kind="text", text=command, page=1, bbox=BBox(x0=0, y0=0, x1=1, y1=1)
                ),
            ),
        )
