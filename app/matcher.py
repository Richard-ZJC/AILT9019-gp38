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
    fuzzy: bool = False
    corrected: str = ""  # query word -> keyword word, shown when fuzzy


def _levenshtein(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment): substitutions,
    insertions, deletions and adjacent transpositions."""
    if a == b:
        return 0
    len_a, len_b = len(a), len(b)
    d = [[0] * (len_b + 1) for _ in range(len_a + 1)]
    for i in range(len_a + 1):
        d[i][0] = i
    for j in range(len_b + 1):
        d[0][j] = j
    for i in range(1, len_a + 1):
        for j in range(1, len_b + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len_a][len_b]


_FUZZY_MAX_DISTANCE = 2
_FUZZY_MIN_LENGTH = 4


def fuzzy_match(text: str, rules: Sequence[Rule]) -> Match | None:
    """Typo rescue, still zero-cost: 'bottel' -> 'plastic bottle'.

    Deliberately conservative, because a wrong auto-correction is worse
    than none:
    - only short queries (<= 3 words) look like typos; full sentences go
      to the AI layer, which can re-judge the whole input;
    - a query word matches a keyword word at Damerau-Levenshtein distance
      1 (words up to 5 chars) or 2 (longer words) - distance 0 is the
      exact layer's business and must not steal underspecified queries.
    """
    all_words = normalise(text).split()
    if not all_words or len(all_words) > 3:
        return None
    words = [word for word in all_words if len(word) >= _FUZZY_MIN_LENGTH]
    if not words:
        return None

    best: Match | None = None
    best_distance = _FUZZY_MAX_DISTANCE + 1
    for rule in rules:
        for keyword in rule.keywords:
            keyword_words = [word for word in normalise(keyword).split() if len(word) >= _FUZZY_MIN_LENGTH]
            for query_word in words:
                limit = 1 if len(query_word) <= 5 else _FUZZY_MAX_DISTANCE
                for keyword_word in keyword_words:
                    distance = _levenshtein(query_word, keyword_word)
                    # distance 0 means the word matches exactly - that is the
                    # exact layer's job; fuzzy must not turn "a bottle" into
                    # "plastic bottle" and steal underspecified queries.
                    if not 1 <= distance <= limit:
                        continue
                    if distance < best_distance:
                        best_distance = distance
                        best = Match(rule=rule, keyword=keyword, score=1, fuzzy=True,
                                     corrected=f"{query_word} -> {keyword}")
    return best


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
