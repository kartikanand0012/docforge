"""Names offered for an unmapped supplier or item: never applied, only offered for a click.

Each candidate is a name the workspace's books already use - from its imported masters, or
from a mapping it made before - and is ranked against what the bill prints:

- a candidate with the bill's seller GSTIN first ("same GSTIN", 1.0);
- then the same words in any case, punctuation or order ("same name", 0.95);
- then shared words, rarer words counting more, a word's first four letters or a strength's
  number counting for most of a word ("similar name", at most 0.9). At least one word must
  be shared, not only a number or strength; a product of another strength ("500mg" for
  "250mg") counts for much less: it is another product.

Built once per request from the workspace's names (an inverted index of their words), so a
50-line bill against 20,000 names is answered in well under the two seconds allowed.
"""

import math
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from docforge.mappings.keys import STRENGTH, gstin_key, name_tokens

Source = Literal["master", "mapping"]
Why = Literal["same GSTIN", "same name", "similar name"]

LIMIT = 5
_FLOOR = 0.5  # below this a name is not offered
_PREFIX = 4
_PARTIAL = 0.8  # a word's first letters, or a strength's number, against the whole word
_OTHER_STRENGTH = 0.4
_REFINED = 25
_PACK_SIZE = re.compile(r"\d+x\d+")  # the best by shared words, compared again letter by letter


@dataclass(frozen=True)
class Candidate:
    name: str  # what would be booked
    gstin: str | None
    source: Source
    match_text: str | None = None  # compared instead of `name`, such as a mapping's print


@dataclass(frozen=True)
class Suggestion:
    name: str
    gstin: str | None
    source: Source
    score: float  # 0 to 1, two places
    why: Why


@dataclass(frozen=True)
class _Entry:
    candidate: Candidate
    tokens: tuple[str, ...]
    strengths: frozenset[str]
    weight: float


def _strengths(tokens: Iterable[str]) -> frozenset[str]:
    return frozenset(t for t in tokens if STRENGTH.fullmatch(t))


def _is_word(token: str) -> bool:
    """Letters that name something: not a number, a strength ("250mg") or a pack ("10x10")."""
    return (
        any(c.isalpha() for c in token)
        and _number(token) is None
        and not _PACK_SIZE.fullmatch(token)
    )


def _number(token: str) -> str | None:
    found = STRENGTH.fullmatch(token)
    if found:
        return found.group(1)
    return token if token.replace(".", "", 1).isdigit() else None


class Suggester:
    def __init__(self, candidates: Iterable[Candidate]) -> None:
        raw = [(c, name_tokens(c.match_text or c.name)) for c in candidates]
        raw = [(c, tokens) for c, tokens in raw if tokens]
        frequency: dict[str, int] = defaultdict(int)
        for _, tokens in raw:
            for token in tokens:
                frequency[token] += 1
        self._count = len(raw)
        self._idf = {t: math.log(1 + self._count / n) for t, n in frequency.items()}
        self._entries: list[_Entry] = []
        self._exact: dict[str, list[int]] = defaultdict(list)
        self._prefix: dict[str, list[int]] = defaultdict(list)
        self._numbers: dict[str, list[int]] = defaultdict(list)
        self._gstins: dict[str, list[int]] = defaultdict(list)
        self._keys: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for index, (candidate, tokens) in enumerate(raw):
            weight = sum(self._idf[t] for t in tokens)
            self._entries.append(_Entry(candidate, tokens, _strengths(tokens), weight))
            self._keys[tuple(sorted(tokens))].append(index)
            for token in tokens:
                self._exact[token].append(index)
                if len(token) >= _PREFIX and token.isalpha():
                    self._prefix[token[:_PREFIX]].append(index)
                number = _number(token)
                if number is not None:
                    self._numbers[number].append(index)
            gstin = gstin_key(candidate.gstin)
            if gstin:
                self._gstins[gstin].append(index)

    def _weight(self, token: str) -> float:
        return self._idf.get(token, math.log(1 + max(self._count, 1)))

    def suggest(
        self, text: str, *, gstin: str | None = None, limit: int = LIMIT
    ) -> list[Suggestion]:
        """At most `limit` names for what a bill prints as `text` (and its seller GSTIN),
        best first; none that is not alike."""
        tokens = name_tokens(text)
        best: dict[str, Suggestion] = {}

        def offer(entry: _Entry, score: float, why: Why) -> None:
            candidate = entry.candidate
            found = best.get(candidate.name)
            if found is None or score > found.score:
                best[candidate.name] = Suggestion(
                    candidate.name, candidate.gstin, candidate.source, round(score, 2), why
                )

        key = gstin_key(gstin)
        for index in self._gstins.get(key, []) if key else []:
            offer(self._entries[index], 1.0, "same GSTIN")
        if tokens:
            for index in self._keys.get(tuple(sorted(tokens)), []):
                offer(self._entries[index], 0.95, "same name")
            for index, score in self._similar(tokens):
                offer(self._entries[index], score, "similar name")
        ranked = sorted(best.values(), key=lambda s: (-s.score, s.name.casefold(), s.name))
        return ranked[:limit]

    def _similar(self, tokens: tuple[str, ...]) -> list[tuple[int, float]]:
        shared: dict[int, float] = defaultdict(float)
        worded: set[int] = set()  # those sharing a word, not only a number or strength
        for token in tokens:
            weight = self._weight(token)
            word = _is_word(token)
            seen: set[int] = set()
            for index in self._exact.get(token, []):
                shared[index] += weight
                seen.add(index)
            if word:
                worded.update(seen)
            partial: list[list[int]] = []
            if len(token) >= _PREFIX and token.isalpha():
                partial.append(self._prefix.get(token[:_PREFIX], []))
            number = _number(token)
            if number is not None:
                partial.append(self._numbers.get(number, []))
            for postings in partial:
                for index in postings:
                    if index not in seen:
                        shared[index] += _PARTIAL * weight
                        seen.add(index)
            if word:
                worded.update(seen)
        if not shared:
            return []
        mine = sum(self._weight(t) for t in tokens)
        strengths = _strengths(tokens)
        dice = {
            i: 2 * s / (mine + self._entries[i].weight) for i, s in shared.items() if i in worded
        }
        text = " ".join(tokens)
        found = []
        for index in sorted(dice, key=lambda i: -dice[i])[:_REFINED]:
            entry = self._entries[index]
            letters = SequenceMatcher(None, text, " ".join(entry.tokens)).ratio()
            score = min(max(dice[index], 0.9 * letters), 0.9)
            if strengths and entry.strengths and strengths != entry.strengths:
                score *= _OTHER_STRENGTH
            if score >= _FLOOR:
                found.append((index, score))
        return found
