"""V2 P3, AC-P3-7: no text the product shows claims more about Marg than is true.

"Marg" goes with "import file" or "purchase import" - a file the workspace's own operator
imports into his own Marg. Never with "direct integration", "auto-posts to Marg", "syncs with
Marg" or "Marg compatible": no import into Marg has been verified yet. Once one is, and it is
recorded in docs/plans/ with its date and Marg version, this test allows such words.

Read here: every string in the API's code (what errors and descriptions can say) and every
text in the published OpenAPI. Comments are not shown to anyone and are not read.
"""

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "src" / "docforge"
OPENAPI = REPO / "docs" / "api" / "openapi.json"
PLANS = REPO / "docs" / "plans"
CLAIM = re.compile(
    r"direct integration|integrates? with|auto-?posts?|posts? (?:it )?(?:in)?to marg"
    r"|sync(?:s|ed|ing)? (?:with|to) marg|marg[- ]compatible|compatible with marg"
    r"|verified (?:with|in|by) marg|marg[- ]verified",
    re.IGNORECASE,
)
# A verified import, as docs/plans/ must record it before any such words are allowed.
VERIFIED = re.compile(r"^Verified Marg import: \d{4}-\d{2}-\d{2}, Marg \S+", re.MULTILINE)


def _strings(path: Path) -> Iterator[tuple[int, str]]:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _texts(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


def _claims(text: str) -> bool:
    return "marg" in text.casefold() and CLAIM.search(text) is not None


def test_no_text_claims_more_than_a_marg_import_file() -> None:
    if any(VERIFIED.search(p.read_text(encoding="utf-8")) for p in PLANS.glob("*.md")):
        return  # a real import is on record; the claim may now be made
    found = [
        f"{path.relative_to(REPO)}:{line}: {text[:120]!r}"
        for path in sorted(SOURCE.rglob("*.py"))
        for line, text in _strings(path)
        if _claims(text)
    ]
    found += [
        f"docs/api/openapi.json: {text[:120]!r}"
        for text in _texts(json.loads(OPENAPI.read_text(encoding="utf-8")))
        if _claims(text)
    ]
    assert found == []


def test_the_scan_finds_a_claim_when_there_is_one() -> None:
    assert _claims("DocForge auto-posts to Marg")
    assert _claims("Direct integration with Marg ERP")
    assert _claims("Marg compatible purchase file")
    assert not _claims("Download the Marg purchase import file")
    assert not _claims("Integrates with Tally")  # no Marg in it
