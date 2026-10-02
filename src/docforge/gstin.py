"""GSTIN (Indian GST identification number) format and check character.

Layout: 2-digit state code, 10-character PAN, entity number, the letter Z, check character.
The check character is a base-36 Luhn-style checksum over the first 14 characters.
"""

import re

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_BASE = len(_ALPHABET)
# The state code is not checked against the list of real states here.
_PATTERN = re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]")


def gstin_check_char(first_14: str) -> str:
    """Check character for the first 14 characters of a GSTIN."""
    if len(first_14) != 14:
        raise ValueError("expected the first 14 characters of a GSTIN")
    total = 0
    for position, char in enumerate(first_14):
        value = _ALPHABET.find(char)
        if value < 0:
            raise ValueError(f"invalid GSTIN character {char!r}")
        product = value * (2 if position % 2 else 1)
        total += product // _BASE + product % _BASE
    return _ALPHABET[(_BASE - total % _BASE) % _BASE]


def is_valid_gstin(gstin: str) -> bool:
    """True when the format and the check character are both correct."""
    if not _PATTERN.fullmatch(gstin):
        return False
    return gstin_check_char(gstin[:14]) == gstin[14]


def make_gstin(state_code: str, pan: str, entity: str = "1") -> str:
    """Build a GSTIN with a correct check character."""
    first_14 = f"{state_code}{pan}{entity}Z"
    return first_14 + gstin_check_char(first_14)
