"""How a supplier and an item are keyed in a workspace's mapping master, and how names are
broken into words for suggestions.

A supplier is its seller GSTIN: every printed variation of its name resolves to one mapping.
An item is its printed product name and pack, compared as `product_key` compares product
names (case, spacing and most punctuation aside); the printed HSN is not part of it, so a bill
that leaves the HSN out books the same item.
"""

import re
import unicodedata

from docforge.trust.duplicates import text_key
from docforge.trust.match import company_key
from docforge.trust.schedule_h1 import product_key

MAX_ITEM_KEY = 420

# Words that say nothing about which supplier or product it is.
_STOP = frozenset(
    {"pvt", "private", "ltd", "limited", "llp", "the", "and", "co", "company", "ms", "m", "s"}
    | {"ip", "bp", "usp"}  # pharmacopoeia marks
)
# A dosage form's spellings, as one word each.
_FORMS = {
    **dict.fromkeys(("tab", "tabs", "tablet", "tablets"), "tab"),
    **dict.fromkeys(("cap", "caps", "capsule", "capsules"), "cap"),
    **dict.fromkeys(("inj", "injection", "injections"), "inj"),
    **dict.fromkeys(("syp", "syr", "syrup"), "syp"),
    **dict.fromkeys(("susp", "suspension"), "susp"),
    **dict.fromkeys(("oint", "ointment"), "oint"),
    **dict.fromkeys(("drop", "drops"), "drop"),
}
_UNIT = r"(?:mg|mcg|ml|gm|g|kg|iu|l|%)"
_SPACED_UNIT = re.compile(rf"(\d)\s+({_UNIT})(?![a-z])")
_PACK = re.compile(r"(\d)\s*[x\u00d7*]\s*(\d)")
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?%?")
STRENGTH = re.compile(rf"(\d+(?:\.\d+)?){_UNIT}")


def gstin_key(text: object) -> str | None:
    """A GSTIN as stored and compared: upper case, without spaces."""
    key = text_key(text)
    return key.upper() if key else None


def supplier_name_key(name: object) -> str | None:
    """A supplier's printed name as compared: case, full stops, commas and spacing aside."""
    return company_key(name)


def item_key(product_name: str | None, pack: str | None) -> str | None:
    """A product as the mapping master keys it: its name and pack. None without a name."""
    name = product_key(product_name) if product_name else ""
    if not name:
        return None
    return f"{name}|{product_key(pack) if pack else ''}"


def name_tokens(text: str) -> tuple[str, ...]:
    """The words of a name that say which supplier or product it is, in order, each once:
    lower case, "250 mg" as "250mg", "10 x 10" as "10x10", one spelling per dosage form."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = _PACK.sub(r"\1x\2", _SPACED_UNIT.sub(r"\1\2", folded))
    seen: dict[str, None] = {}
    for token in _TOKEN.findall(folded):
        word = _FORMS.get(token, token)
        if word not in _STOP:
            seen.setdefault(word, None)
    return tuple(seen)
