"""The parser in a child process: a file that hangs, hoards memory or crashes costs one
document, not the worker."""

import os
from collections.abc import Iterator

import psutil
import pytest

from docforge.parsing.base import DocumentTooLarge, NoTextLayer, ParseError
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

    with pytest.raises(ParseError, match="stopped unexpectedly"):
        parser.parse(b"exit")

    assert pid(parser) != first


def test_a_parser_that_takes_too_long_is_stopped(parser: IsolatedParser) -> None:
    quick = IsolatedParser(CommandParser, name="command", version="0", timeout_seconds=1.0)
    try:
        first = pid(quick)
        with pytest.raises(ParseError, match="time limit"):
            quick.parse(b"sleep")
        assert not psutil.pid_exists(first) or psutil.Process(first).status() == "zombie"
        assert pid(quick) != first
    finally:
        quick.close()


def test_a_parser_that_takes_too_much_memory_is_stopped(parser: IsolatedParser) -> None:
    first = pid(parser)

    with pytest.raises(ParseError, match="memory limit"):
        parser.parse(b"hoard")

    assert pid(parser) != first


def test_closing_stops_the_process(parser: IsolatedParser) -> None:
    child = pid(parser)

    parser.close()

    assert not psutil.pid_exists(child) or psutil.Process(child).status() == "zombie"
    assert pid(parser) != child  # and it starts again on demand
