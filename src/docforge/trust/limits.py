"""Specifications on a certificate of analysis, read into limits, and results checked.

Handles the forms that cover most pharmacopoeial specifications: a range ("95.0 - 105.0 %",
"Between 3.5 and 5.5"), an upper limit ("NMT 1.0 %", "Not more than", "≤"), a lower limit
("NLT 80 %", "Not less than", "≥"), and a required text ("Complies", "Positive", "Sterile").
Anything else is not read, and a check against it is "not evaluated": a person looks.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict

Outcome = Literal["passed", "failed", "not_evaluated"]

_NUMBER = r"(\d+(?:\.\d+)?)"
_UNIT = r"\s*((?:%\s*w/w|%\s*w/v|%|[A-Za-z]+(?:/[A-Za-z]+)?)?)"
_RANGE = re.compile(
    rf"^(?:between\s+)?{_NUMBER}\s*%?\s*(?:-|\u2013|\u2014|to|and)\s*{_NUMBER}{_UNIT}\s*$",
    re.IGNORECASE,
)
# Read in full or not at all: a second limit, a qualifier or a decimal comma after the first
# number must not be dropped (a result would then be checked against half the specification).
# The one qualifier allowed is the dissolution one, "(Q) in 45 min", which does not change it.
_TAIL = r"(?:\s*\(Q\)(?:\s+in\s+\d+\s*min(?:utes)?)?)?\s*$"
_MAX = re.compile(rf"^(?:nmt|not more than|≤|<=)\s*{_NUMBER}{_UNIT}{_TAIL}", re.IGNORECASE)
_MIN = re.compile(rf"^(?:nlt|not less than|≥|>=)\s*{_NUMBER}{_UNIT}{_TAIL}", re.IGNORECASE)
_RESULT = re.compile(rf"^{_NUMBER}{_UNIT}\s*$")
# Texts a specification may require, and the results that satisfy each.
_TEXTS = {
    "complies": {"complies", "conforms"},
    "positive": {"positive"},
    "negative": {"negative"},
    "sterile": {"sterile", "complies", "conforms"},
    "absent": {"absent", "complies"},
}


class Limit(BaseModel):
    model_config = ConfigDict(frozen=True)

    low: Decimal | None = None
    high: Decimal | None = None
    unit: str = ""
    text: str | None = None  # a required wording instead of a number


def _unit(raw: str) -> str:
    return " ".join(raw.split())


def parse_limit(spec: str) -> Limit | None:
    text = " ".join(spec.split())
    if not text:
        return None
    if text.lower() in _TEXTS:
        return Limit(text=text.lower())
    try:
        if match := _RANGE.match(text):
            low, high = Decimal(match[1]), Decimal(match[2])
            return Limit(low=low, high=high, unit=_unit(match[3])) if low <= high else None
        if match := _MAX.match(text):
            return Limit(high=Decimal(match[1]), unit=_unit(match[2]))
        if match := _MIN.match(text):
            return Limit(low=Decimal(match[1]), unit=_unit(match[2]))
    except InvalidOperation:
        return None
    return None


def parse_result(result: str) -> tuple[Decimal, str] | None:
    match = _RESULT.match(" ".join(result.split()))
    if match is None:
        return None
    try:
        return Decimal(match[1]), _unit(match[2])
    except InvalidOperation:
        return None


def check_result(spec: str, result: str) -> Outcome:
    limit = parse_limit(spec)
    if limit is None:
        return "not_evaluated"
    if limit.text is not None:
        given = " ".join(result.lower().split())
        if given in _TEXTS[limit.text]:
            return "passed"
        return "failed" if given else "not_evaluated"
    read = parse_result(result)
    if read is None or read[1] != limit.unit:
        return "not_evaluated"
    value = read[0]
    if limit.low is not None and value < limit.low:
        return "failed"
    if limit.high is not None and value > limit.high:
        return "failed"
    return "passed"
