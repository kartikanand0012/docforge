"""A Drive folder as a person names it: its id, or a link to it. Parsed, never fetched."""

import re
from urllib.parse import parse_qs, urlsplit

_ID = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
_PATH = re.compile(r"^/drive/(?:u/\d+/)?folders/([^/]+)/?$")


class InvalidFolder(ValueError):
    """Not a Drive folder id or a Drive folder link."""


def parse_folder(reference: str) -> str:
    """The folder id. Only Google Drive's own https links are read; an id is checked so it
    can be put in a Drive query safely."""
    text = reference.strip()
    if _ID.match(text):
        return text
    if text.startswith("drive.google.com/"):
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme != "https" or parts.hostname != "drive.google.com":
        raise InvalidFolder("not a Google Drive folder link")
    found = _PATH.match(parts.path)
    candidate = found.group(1) if found else None
    if candidate is None and parts.path == "/open":
        candidate = (parse_qs(parts.query).get("id") or [None])[0]
    if candidate is None or not _ID.match(candidate):
        raise InvalidFolder("not a Google Drive folder link")
    return candidate
