"""Text in a document that addresses an AI model rather than a reader.

A document is data. A passage that tries to direct the model ("ignore the previous
instructions") is not read as evidence by chat, and is withheld from AI agents. A heuristic,
not a guard: what keeps an answer honest is that its quotes and figures are checked.
"""

import re
import unicodedata

# Wording that addresses the model, not a reader: "ignore the previous instructions", "you
# are now an assistant that...", "reveal your system prompt". Ordinary letters ("you are now
# entitled to a refund") and documents about AI ("the system prompt was reviewed") are not it.
# A heuristic, not a guard: the quote and statement checks are what keep an answer honest.
_INSTRUCTIONS = re.compile(
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+)*"
    r"(?:previous|prior|above|earlier|preceding|system|original)\s+"
    r"(?:instructions|rules|prompts?|guidance|directions)\b"
    r"|\b(?:reveal|print|show|repeat|output|ignore|override)\s+(?:the\s+|your\s+)?"
    r"(?:system\s+prompt|developer\s+message|hidden\s+instructions)\b"
    r"|\byou\s+(?:are|will\s+be)\s+now\s+(?:an?\s+)?(?:\w+\s+){0,2}"
    r"(?:assistant|ai|model|chatbot|bot)\b"
    r"|\byou\s+(?:must|will|should)\s+now\s+(?:say|answer|reply|respond|tell|state|write)\b"
    r"|\b(?:as\s+an?\s+)?(?:ai|assistant|language\s+model),?\s+(?:you\s+)?(?:must|should)\b",
    re.IGNORECASE,
)
# Characters that do not show, used to break a phrase up so a pattern misses it: every format
# character (Unicode category Cf: zero-width, bidi marks and isolates, the tag characters),
# controls other than line breaks and tabs, variation selectors, and the fillers that render
# as nothing. Combining marks are kept: Hindi and accented text need them.
_FILLERS = frozenset("\u034f\u115f\u1160\u3164\uffa0")
# Latin look-alikes in other scripts, read as the letters they show (for finding only).
_LOOK_ALIKES = str.maketrans(
    "\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u0456\u0455\u0458\u04cf"
    "\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0425"
    "\u03bf\u03b1\u03b5\u03b9\u039f\u0391\u0395\u0399",
    "aeopcyxisjlABEKMHOPCTXoaeiOAEI",
)


def _hidden(char: str) -> bool:
    if char in "\n\t\r":
        return False
    if char in _FILLERS or "\ufe00" <= char <= "\ufe0f" or "\U000e0100" <= char <= "\U000e01ef":
        return True
    return unicodedata.category(char) in ("Cf", "Cc")


def shown(text: str) -> str:
    """The text as printed, without the characters a reader cannot see. Nothing else changes:
    units, exponents and symbols (m\u00b2, 10\u207b\u00b3, \u00b5g) are a document's data."""
    return "".join(char for char in text if not _hidden(char))


def reads_as_instructions(text: str) -> bool:
    """A passage that reads like instructions to the model rather than document content.
    Read as a person would: hidden characters removed, compatibility forms folded (full-width
    letters) and look-alike letters of other scripts read as Latin."""
    folded = unicodedata.normalize("NFKC", shown(text)).translate(_LOOK_ALIKES)
    return _INSTRUCTIONS.search(shown(folded)) is not None
