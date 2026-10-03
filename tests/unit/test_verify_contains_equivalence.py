"""`_contains` without regular expressions must answer exactly as the regex it replaced.

The regex was built per value and overflowed Python's pattern cache in the review queue
(12,000 values per call); the replacement scans with str.find. This compares the two on
generated cases built from the characters the boundary rules care about.
"""

import random
import re

from docforge.trust.verify import _contains


def regex_contains(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    before = r"(?<![0-9A-Za-z])(?<![0-9][.,/-])"
    if needle[0].isdigit():
        before += r"(?<![-+(])"
    pattern = before + re.escape(needle) + r"(?![0-9A-Za-z])(?![.,/-][0-9])"
    return re.search(pattern, haystack) is not None


ALPHABET = "05aZ.,/-+( é٣"  # digits, letters, the separators, a non-ASCII letter and digit


def test_it_answers_as_the_regex_did_on_generated_cases() -> None:
    rng = random.Random(20261003)  # noqa: S311 - test data, not secrets
    for _ in range(40_000):
        needle = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 4)))
        haystack = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 12)))
        if rng.random() < 0.5 and needle:
            at = rng.randint(0, len(haystack))
            haystack = haystack[:at] + needle + haystack[at:]
        assert _contains(haystack, needle) == regex_contains(haystack, needle), (haystack, needle)


def test_the_documented_cases() -> None:
    assert not _contains("200", "20")
    assert not _contains("1,166.40", "166.40")
    assert not _contains("86.245", "86.24")
    assert not _contains("05/29/2028", "05/29")
    assert not _contains("-5", "5")
    assert not _contains("(5.00)", "5.00")
    assert _contains("Total 5.00 INR", "5.00")
