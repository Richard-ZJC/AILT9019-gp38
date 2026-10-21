"""All refusal copy lives here, so every refusal reads the same way.

Four reasons to refuse, one shape of answer. Nothing in this module ever
guesses a category; refusing is always the safe default.
"""

from __future__ import annotations

import re
from typing import Any

from .matcher import normalise

OUT_OF_SCOPE = "This packaging is not covered by the current rule set."
SERVICE_DOWN = "This request involves sensitive information and cannot be processed right now."
TOO_MANY_QUESTIONS = (
    "I still cannot identify the packaging after two rounds of questions. "
    "Please check the item yourself, or take it to the re Kiosk counter for help."
)
HAZARD = (
    "This item looks hazardous or bulky, which is outside the campus packaging recycling rules. "
    "Do not put it in a recycling bin - contact the Estates Office or a specialist collection point."
)

# Second net, on top of data/rules.json: items that must never reach the AI path.
_HAZARD_PATTERNS = ("chemical", "solvent", "pesticide", "asbestos", "syringe", "medical waste", "clinical waste", "aerosol")


def hazard_hit(text: str) -> str | None:
    haystack = normalise(text)
    for pattern in _HAZARD_PATTERNS:
        if re.search(rf"(?<![a-z0-9]){pattern}", haystack):
            return pattern
    return None


def payload(kind: str, reason: str, **extra: Any) -> dict[str, Any]:
    message = {
        "out_of_scope": OUT_OF_SCOPE,
        "service_unavailable": SERVICE_DOWN,
        "too_many_questions": TOO_MANY_QUESTIONS,
        "hazard": HAZARD,
    }.get(kind, OUT_OF_SCOPE)
    body: dict[str, Any] = {
        "status": "REFUSE" if kind != "service_unavailable" else "SERVICE_UNAVAILABLE",
        "message": message,
        "reason": reason,
    }
    body.update(extra)
    return body
