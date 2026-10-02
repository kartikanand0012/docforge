from pathlib import Path

import pytest

from docforge.parsing.base import ParseError
from docforge.parsing.cache import CachingParser
from fakes import PARSED, FakeParser


def test_a_miss_parses_and_stores_then_a_hit_skips_the_parser(tmp_path: Path) -> None:
    inner = FakeParser()
    parser = CachingParser(tmp_path, inner)

    first = parser.parse(b"pdf one")
    second = parser.parse(b"pdf one")

    assert first == second == PARSED
    assert inner.calls == 1
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_different_files_are_cached_separately(tmp_path: Path) -> None:
    inner = FakeParser()
    parser = CachingParser(tmp_path, inner)

    parser.parse(b"pdf one")
    parser.parse(b"pdf two")

    assert inner.calls == 2
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_replay_only_serves_what_was_cached_earlier(tmp_path: Path) -> None:
    CachingParser(tmp_path, FakeParser()).parse(b"pdf one")

    assert CachingParser(tmp_path).parse(b"pdf one") == PARSED


def test_replay_only_fails_on_a_miss(tmp_path: Path) -> None:
    with pytest.raises(ParseError, match="no cached parse"):
        CachingParser(tmp_path).parse(b"pdf one")


def test_a_cached_parse_from_another_parser_version_is_redone(tmp_path: Path) -> None:
    CachingParser(tmp_path, FakeParser()).parse(b"pdf one")
    newer = FakeParser()
    newer.version = "1"

    CachingParser(tmp_path, newer).parse(b"pdf one")

    assert newer.calls == 1


def test_reports_the_inner_parser_identity(tmp_path: Path) -> None:
    assert CachingParser(tmp_path, FakeParser()).name == "fake"
    assert CachingParser(tmp_path).name == "cached"


def test_a_truncated_cache_file_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    inner = FakeParser()
    parser = CachingParser(tmp_path, inner)
    parser.parse(b"pdf one")
    (cached,) = tmp_path.glob("*.json")
    cached.write_text('{"parser": "fa', encoding="utf-8")

    assert parser.parse(b"pdf one") == PARSED
    assert inner.calls == 2
    assert list(tmp_path.glob("*.tmp")) == []
