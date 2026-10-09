"""Cache parser output on disk, keyed by the file's content hash.

Evals replay cached parses so they run without the parser's models and give the same
result on every machine.
"""

import hashlib
import os
from pathlib import Path

from docforge.parsing.base import ParsedDocument, ParseError, Parser


class NotCached(ParseError):
    """Replay only, and no parse was recorded for this file: reading it again will not help."""


class CachingParser:
    """Serves cached parses. With `inner`, a miss is parsed and stored.

    Without `inner` it is replay-only: a miss raises instead of parsing.
    """

    def __init__(self, directory: Path, inner: Parser | None = None) -> None:
        self.name = inner.name if inner is not None else "cached"
        self.version = inner.version if inner is not None else "cached"
        self._directory = directory
        self._inner = inner

    def _is_current(self, document: ParsedDocument) -> bool:
        inner = self._inner
        return inner is None or (document.parser, document.parser_version) == (
            inner.name,
            inner.version,
        )

    def parse(self, pdf: bytes) -> ParsedDocument:
        key = hashlib.sha256(pdf).hexdigest()
        path = self._directory / f"{key[:24]}.json"
        cached = self._read(path)
        if cached is not None and self._is_current(cached):
            return cached
        if self._inner is None:
            raise NotCached(f"no cached parse for this file (sha256 {key[:24]})")
        document = self._inner.parse(pdf)
        self._directory.mkdir(parents=True, exist_ok=True)
        # Write then rename, so an interrupted run never leaves half a file behind.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(document.model_dump_json() + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return document

    @staticmethod
    def _read(path: Path) -> ParsedDocument | None:
        """The cached parse, or None if there is none or the file is damaged."""
        try:
            return ParsedDocument.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
