"""Zero-cost layer, level 1: exact keyword matching against rules.json.

A hit here means the request never leaves the machine - no API call is made.
Matching is case-insensitive and punctuation-insensitive, but word-bounded so
"can" does not fire inside "candy".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .rules_loader import Rule

_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> str:
    """Lower-case, collapse every non-alphanumeric run into a single space."""
    return _NON_WORD.sub(" ", (text or "").lower()).strip()


def _pattern(keyword: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])")


def keyword_hit(text: str, keyword: str) -> bool:
    needle = normalise(keyword)
    if not needle:
        return False
    return bool(_pattern(needle).search(text))


@dataclass(frozen=True)
class Match:
    rule: Rule
    keyword: str
    score: int


def match(text: str, rules: Sequence[Rule]) -> Match | None:
    """Return the single best exact match, or None.

    Scoring: a keyword with more words wins (more specific), so
    "plastic bottle" beats "bottle" in the same sentence.
    """
    haystack = normalise(text)
    if not haystack:
        return None

    best: Match | None = None
    for rule in rules:
        for keyword in rule.keywords:
            if not keyword_hit(haystack, keyword):
                continue
            score = len(normalise(keyword).split())
            if best is None or score > best.score:
                best = Match(rule=rule, keyword=keyword, score=score)
    return best


def match_all(text: str, rules: Sequence[Rule]) -> list[Match]:
    """Every rule that hit, best first. Used for prompt candidate injection."""
    haystack = normalise(text)
    if not haystack:
        return []
    hits: list[Match] = []
    for rule in rules:
        best_keyword = ""
        best_score = 0
        for keyword in rule.keywords:
            if keyword_hit(haystack, keyword):
                score = len(normalise(keyword).split())
                if score > best_score:
                    best_keyword, best_score = keyword, score
        if best_score:
            hits.append(Match(rule=rule, keyword=best_keyword, score=best_score))
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits


def candidates(text: str, rules: Iterable[Rule], top_k: int = 3) -> list[Rule]:
    return [hit.rule for hit in match_all(text, list(rules))[:top_k]]
