"""Cache parser output on disk, keyed by the file's content hash.

Evals replay cached parses so they run without the parser's models and give the same
result on every machine.
"""

import hashlib
from pathlib import Path

from docforge.parsing.base import ParsedDocument, ParseError, Parser


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
        if path.exists():
            cached = ParsedDocument.model_validate_json(path.read_text(encoding="utf-8"))
            if self._is_current(cached):
                return cached
        if self._inner is None:
            raise ParseError(f"no cached parse for this file (sha256 {key[:24]})")
        document = self._inner.parse(pdf)
        self._directory.mkdir(parents=True, exist_ok=True)
        path.write_text(document.model_dump_json() + "\n", encoding="utf-8")
        return document
