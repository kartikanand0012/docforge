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
# Characters that do not show, used to break a phrase up so a pattern misses it; the Unicode
# tag characters (U+E0000 to U+E007F) can also carry whole hidden text.
_INVISIBLE = re.compile(
    "[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\U000e0000-\U000e007f]"
)


def shown(text: str) -> str:
    """The text as a reader sees it: compatibility forms folded (full-width letters) and
    characters that do not show removed."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))


def reads_as_instructions(text: str) -> bool:
    """A passage that reads like instructions to the model rather than document content.
    Read as shown: compatibility forms folded (full-width letters) and invisible
    characters removed."""
    return _INSTRUCTIONS.search(shown(text)) is not None
