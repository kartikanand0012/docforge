"""Run a parser in a child process, with limits.

Layout and OCR models are native code working on files we did not write. A file that makes
them hang, grow without bound or crash should cost that one document, not the worker. The
child is also replaced after a number of documents, because the models' memory use creeps.
"""

import json
import multiprocessing
import os
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess

import psutil

from docforge.parsing.base import (
    DocumentTooLarge,
    NoTextLayer,
    ParsedDocument,
    ParseError,
    Parser,
    ParserLimitExceeded,
)

_ERRORS: dict[str, type[ParseError]] = {
    error.__name__: error for error in (ParseError, DocumentTooLarge, NoTextLayer)
}
_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "DATABASE_URL")
_POLL_SECONDS = 0.2
_GIB = 1024**3


def _serve(factory: Callable[[], Parser], connection: Connection) -> None:
    """The child: parse each file received and send back the result or the error."""
    # The parser needs no credentials, and it runs native code on files we did not write.
    for name in list(os.environ):
        if any(marker in name.upper() for marker in _SECRET_MARKERS):
            del os.environ[name]
    parser: Parser | None = None
    while True:
        try:
            pdf = connection.recv_bytes()
        except EOFError:
            return
        try:
            if parser is None:
                parser = factory()
            reply = ("ok", parser.parse(pdf).model_dump_json())
        except ParseError as error:
            reply = (type(error).__name__, str(error))
        except Exception as error:
            reply = ("ParseError", f"the parser failed with {type(error).__name__}: {error}")
        # JSON, not pickle: the parent must be able to read a reply without trusting it.
        connection.send_bytes(json.dumps(reply).encode())


class IsolatedParser:
    """A `Parser` whose work happens in a child process built by `factory`.

    `factory` must be importable by name (a class or a `functools.partial` of one), because
    the child is started fresh rather than forked. One document is parsed at a time. A parse
    that exceeds the time or memory limit, or whose process dies, raises `ParserLimitExceeded` and
    the next document gets a new process.
    """

    def __init__(
        self,
        factory: Callable[[], Parser],
        *,
        name: str,
        version: str,
        max_documents: int = 50,
        timeout_seconds: float = 900.0,
        max_rss_bytes: int = 8 * _GIB,
    ) -> None:
        self.name = name
        self.version = version
        self._factory = factory
        self._max_documents = max_documents
        self._timeout_seconds = timeout_seconds
        self._max_rss_bytes = max_rss_bytes
        self._lock = threading.Lock()
        self._process: SpawnProcess | None = None
        self._connection: Connection | None = None
        self._served = 0
        self.peak_rss_bytes = 0  # highest memory use seen in any child, for measurement

    def parse(self, pdf: bytes) -> ParsedDocument:
        with self._lock:
            process, connection = self._ready()
            self._served += 1
            deadline = time.monotonic() + self._timeout_seconds
            try:
                connection.send_bytes(pdf)
                while not connection.poll(_POLL_SECONDS):
                    if not process.is_alive():
                        raise ParserLimitExceeded(
                            "Parsing was stopped: the parser process stopped unexpectedly "
                            f"(exit code {process.exitcode})."
                        )
                    if time.monotonic() > deadline:
                        raise ParserLimitExceeded(
                            "Parsing was stopped: it exceeded the time limit of "
                            f"{self._timeout_seconds:.0f} s."
                        )
                    used = self._rss(process)
                    if used > self._max_rss_bytes:
                        raise ParserLimitExceeded(
                            "Parsing was stopped: it exceeded the memory limit of "
                            f"{self._max_rss_bytes // 1024**2} MB."
                        )
                kind, payload = json.loads(connection.recv_bytes())
            except ParserLimitExceeded:
                self._stop()
                raise
            except (EOFError, OSError, ValueError) as error:
                self._stop()
                raise ParserLimitExceeded(
                    "Parsing was stopped: the parser process stopped unexpectedly."
                ) from error
            except BaseException:
                # Interrupted mid-exchange: a reply left in the pipe must never be read as
                # the answer for the next document.
                self._stop()
                raise
        if kind != "ok":
            raise _ERRORS.get(kind, ParseError)(payload)
        return ParsedDocument.model_validate_json(payload)

    def close(self) -> None:
        """Stop the child. The next `parse` starts a new one.

        Does not wait for a parse that is under way: it kills the process, and that parse
        fails with `ParserLimitExceeded`.
        """
        process = self._process
        if process is not None and process.is_alive():
            process.kill()
        if self._lock.acquire(timeout=10):
            try:
                self._stop()
            finally:
                self._lock.release()

    def _ready(self) -> tuple[SpawnProcess, Connection]:
        """The running child, replaced first if it has died or served its share."""
        process, connection = self._process, self._connection
        if process is not None and connection is not None:
            if process.is_alive() and self._served < self._max_documents:
                return process, connection
            self._stop()
        context = multiprocessing.get_context("spawn")
        ours, theirs = context.Pipe()
        process = context.Process(target=_serve, args=(self._factory, theirs), daemon=True)
        process.start()
        theirs.close()
        self._process, self._connection, self._served = process, ours, 0
        return process, ours

    def _rss(self, process: SpawnProcess) -> int:
        if process.pid is None:
            return 0
        try:
            used = int(psutil.Process(process.pid).memory_info().rss)
        except psutil.Error:
            return 0
        self.peak_rss_bytes = max(self.peak_rss_bytes, used)
        return used

    def _stop(self) -> None:
        process, connection = self._process, self._connection
        self._process = self._connection = None
        if connection is not None:
            connection.close()
        if process is not None:
            if process.is_alive():
                process.kill()
            process.join(timeout=5)
