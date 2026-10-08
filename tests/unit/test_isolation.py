"""The parser in a child process: a file that hangs, hoards memory or crashes costs one
document, not the worker."""

import inspect
import os
import threading
import time
from collections.abc import Iterator

import psutil
import pytest

from docforge.parsing import isolation
from docforge.parsing.base import (
    DocumentTooLarge,
    NoTextLayer,
    ParseError,
    ParserLimitExceeded,
)
from docforge.parsing.isolation import IsolatedParser
from isolation_parsers import CommandParser


@pytest.fixture
def parser() -> Iterator[IsolatedParser]:
    isolated = IsolatedParser(
        CommandParser,
        name="command",
        version="0",
        max_documents=3,
        timeout_seconds=20.0,
        max_rss_bytes=200 * 1024 * 1024,
    )
    yield isolated
    isolated.close()


def pid(parser: IsolatedParser, text: str = "hello") -> int:
    return int(parser.parse(text.encode()).parser_version)


def test_the_document_is_parsed_in_another_process(parser: IsolatedParser) -> None:
    parsed = parser.parse(b"hello")

    assert parsed.blocks[0].text == "hello"
    assert int(parsed.parser_version) != os.getpid()
    assert (parser.name, parser.version) == ("command", "0")


def test_the_same_process_serves_several_documents_and_is_then_replaced(
    parser: IsolatedParser,
) -> None:
    pids = [pid(parser) for _ in range(4)]

    assert pids[0] == pids[1] == pids[2]  # max_documents is 3
    assert pids[3] != pids[0]
    assert not psutil.pid_exists(pids[0]) or psutil.Process(pids[0]).status() == "zombie"


@pytest.mark.parametrize(
    ("command", "error"),
    [("no-text", NoTextLayer), ("too-large", DocumentTooLarge), ("unreadable", ParseError)],
)
def test_the_parsers_own_errors_arrive_as_the_same_errors(
    parser: IsolatedParser, command: str, error: type[ParseError]
) -> None:
    with pytest.raises(error) as raised:
        parser.parse(command.encode())

    assert type(raised.value) is error
    assert pid(parser) > 0  # and the process is still usable


def test_an_unexpected_error_in_the_parser_is_a_parse_error(parser: IsolatedParser) -> None:
    with pytest.raises(ParseError, match="RuntimeError"):
        parser.parse(b"bug")


def test_a_parser_that_dies_fails_that_document_only(parser: IsolatedParser) -> None:
    first = pid(parser)

    with pytest.raises(ParserLimitExceeded, match="stopped unexpectedly"):
        parser.parse(b"exit")

    assert pid(parser) != first


def test_a_parser_that_takes_too_long_is_stopped(parser: IsolatedParser) -> None:
    quick = IsolatedParser(CommandParser, name="command", version="0", timeout_seconds=1.0)
    try:
        first = pid(quick)
        with pytest.raises(ParserLimitExceeded, match="time limit"):
            quick.parse(b"sleep")
        assert not psutil.pid_exists(first) or psutil.Process(first).status() == "zombie"
        assert pid(quick) != first
    finally:
        quick.close()


def test_a_parser_that_takes_too_much_memory_is_stopped(parser: IsolatedParser) -> None:
    first = pid(parser)

    with pytest.raises(ParserLimitExceeded, match="memory limit"):
        parser.parse(b"hoard")

    assert pid(parser) != first


def test_a_document_over_the_limit_in_a_used_process_is_tried_again_in_a_fresh_one(
    parser: IsolatedParser,
) -> None:
    """Memory an earlier document left behind must not fail the next one (V1 finding F3):
    only a document over the limit in a fresh process is over the limit itself."""
    parser.parse(b"keep:110")  # about 140 MB now held: under three quarters of the limit

    parsed = parser.parse(b"spike:80")  # 220 MB here; 110 MB in a fresh process

    assert parsed.blocks[0].text == "spike:80"


def test_a_process_left_near_the_limit_is_replaced_before_the_next_document(
    parser: IsolatedParser,
) -> None:
    first = int(parser.parse(b"keep:140").parser_version)  # about 170 MB of 200 MB

    assert pid(parser) != first


def test_closing_stops_the_process(parser: IsolatedParser) -> None:
    child = pid(parser)

    parser.close()

    assert not psutil.pid_exists(child) or psutil.Process(child).status() == "zombie"
    assert pid(parser) != child  # and it starts again on demand


def test_the_parser_process_does_not_inherit_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("GEMINI_API_KEY", "DATABASE_URL", "S3_SECRET_KEY", "SOME_TOKEN"):
        monkeypatch.setenv(name, "secret-value")
    monkeypatch.setenv("HARMLESS_SETTING", "kept")
    isolated = IsolatedParser(CommandParser, name="command", version="0")
    try:
        seen = isolated.parse(b"env").blocks[0].text
    finally:
        isolated.close()

    assert "secret-value" not in seen
    assert "HARMLESS_SETTING" in seen


def test_replies_cross_the_process_boundary_as_json_not_pickle() -> None:
    """A parser process taken over by a hostile file must not be able to run code in the parent."""
    source = inspect.getsource(isolation)

    assert "connection.recv()" not in source
    assert ".send(" not in source


def test_closing_from_another_thread_stops_a_parse_that_is_under_way() -> None:
    isolated = IsolatedParser(CommandParser, name="command", version="0", timeout_seconds=60.0)
    pid(isolated)  # started
    closer = threading.Timer(0.5, isolated.close)
    closer.start()
    started = time.monotonic()
    try:
        with pytest.raises(ParserLimitExceeded):
            isolated.parse(b"sleep")
    finally:
        closer.join()
        isolated.close()

    assert time.monotonic() - started < 10


def test_the_parser_process_can_be_given_settings_of_its_own() -> None:
    isolated = IsolatedParser(
        CommandParser, name="command", version="0", child_env={"HF_HUB_OFFLINE": "1"}
    )
    try:
        seen = isolated.parse(b"env").blocks[0].text
    finally:
        isolated.close()

    assert "HF_HUB_OFFLINE=1" in seen
