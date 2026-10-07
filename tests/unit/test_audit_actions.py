"""What the audit-log screen may show of each entry: an allow-list per action.

Every action the code appends is registered with a label and the detail keys that may be
shown. A key not on the list is not sent, and the entry says how many were held back - a
detail added later fails closed until someone decides it may be seen.
"""

import re
from pathlib import Path

from docforge.audit_actions import ACTIONS, shown

SOURCE = Path(__file__).resolve().parents[2] / "src" / "docforge"
# The action of `self._audit(session, document, actor, "<action>", ...)` and of
# `audit.append(..., action="<action>", ...)`.
_AUDIT = re.compile(r'_audit\(\s*session,\s*\w+,\s*[\w.]+,\s*"([a-z_]+\.[a-z_]+)"')
_APPEND = re.compile(r'action="([a-z_]+\.[a-z_]+)"')


def test_every_action_the_code_appends_is_registered_with_a_label() -> None:
    found: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        found |= set(_AUDIT.findall(text)) | set(_APPEND.findall(text))
    assert found, "the scan found no actions: it is broken, not the code"
    assert found <= set(ACTIONS), sorted(found - set(ACTIONS))
    assert all(action.label for action in ACTIONS.values())


def test_only_the_allowed_details_are_shown_and_the_rest_are_counted() -> None:
    visible, hidden = shown("review.signed", {"outcome": "approved", "secret_thing": "x"})
    assert visible == {"outcome": "approved"} and hidden == 1
    visible, hidden = shown("no.such_action", {"a": 1, "b": 2})
    assert visible == {} and hidden == 2


def test_nothing_that_could_be_secret_or_personal_is_allowed() -> None:
    risky = re.compile(r"pin|token|secret|password|text|filename|url|payload|question", re.I)
    allowed = {key for action in ACTIONS.values() for key in action.shown}
    assert not {key for key in allowed if risky.search(key)}
