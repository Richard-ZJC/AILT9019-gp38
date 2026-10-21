"""Optional fuzzy matching (should-have T12). Interface only.

Returning None means "no opinion", so the pipeline falls through to the AI path.
"""

from __future__ import annotations

from typing import Sequence

from .matcher import Match
from .rules_loader import Rule

THRESHOLD = 0.8


def match(text: str, rules: Sequence[Rule]) -> Match | None:
    if not text:
        return None
    return None
