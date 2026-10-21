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
    corrected: str = ""  # human-readable loose-match note, shown when fuzzy
    kind: str = "exact"  # "exact" | "typo" | "overlap"


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
_OVERLAP_MIN_LENGTH = 3
_STOPWORDS = {"the", "and", "with", "from", "for", "that", "this", "have", "just"}


def _keyword_word_index(rules: Sequence[Rule]) -> dict[str, set[str]]:
    """Word -> rule ids whose keywords contain that word (whole-word)."""
    index: dict[str, set[str]] = {}
    for rule in rules:
        for keyword in rule.keywords:
            for word in normalise(keyword).split():
                index.setdefault(word, set()).add(rule.id)
    return index


def _overlap_match(words: Sequence[str], rules: Sequence[Rule]) -> Match | None:
    """'nissin' alone -> the rule whose keyword phrase contains it.

    A query word that appears as a whole word inside a keyword phrase is a
    partial hit - but only worth answering when the word is distinctive:
    if the same word shows up in several rules' keywords ('bottle' lives in
    plastic bottle AND glass bottle) the choice is a guess, so return None
    and let the AI layer re-judge or clarify instead.
    """
    index = _keyword_word_index(rules)
    word_rules: dict[str, set[str]] = {}
    for word in words:
        if word in index and len(index[word]) == 1:
            word_rules[word] = index[word]

    if not word_rules:
        return None
    rule_ids = {next(iter(ids)) for ids in word_rules.values()}
    if len(rule_ids) != 1:
        # two distinctive words disagree (e.g. 'coffee sleeve') - ambiguous
        return None

    rule_id = rule_ids.pop()
    rule = next(rule for rule in rules if rule.id == rule_id)
    hit_word, _ = max(word_rules.items(), key=lambda item: len(item[0]))
    keyword = next(
        (kw for kw in rule.keywords if hit_word in normalise(kw).split()),
        rule.keywords[0],
    )
    return Match(
        rule=rule,
        keyword=keyword,
        score=1,
        fuzzy=True,
        corrected=f'"{hit_word}" matched one word of "{keyword}"',
        kind="overlap",
    )


def fuzzy_match(text: str, rules: Sequence[Rule]) -> Match | None:
    """Loose matching, still zero-cost. Two stages:

    1. word overlap - 'nissin' -> 'nissin cup' (distinctive words only;
       words shared by several rules are ambiguous and go to the AI layer);
    2. typo rescue - 'bottel' -> 'plastic bottle' via Damerau-Levenshtein,
       short queries (<= 3 words) only; distance 0 is the exact layer's
       business and must not steal underspecified queries.

    Refusal rules never fuzzy-match: 'Nestle' is 2 edits from the refusal
    keyword 'needle', and a spelling guess must never refuse a real query.
    Hazards are still caught by the exact layer and refusal.hazard_hit.
    """
    all_words = normalise(text).split()
    if not all_words or len(all_words) > 3:
        return None

    rules = [rule for rule in rules if not rule.is_refusal]
    overlap_words = [
        word for word in all_words
        if len(word) >= _OVERLAP_MIN_LENGTH and word not in _STOPWORDS
    ]
    overlap = _overlap_match(overlap_words, rules) if overlap_words else None
    if overlap is not None:
        return overlap

    index = _keyword_word_index(rules)

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
                    # same distinctiveness rule as overlap: correcting into a
                    # word that several rules share ('bottle') is a coin toss
                    if len(index.get(keyword_word, ())) != 1:
                        continue
                    if distance < best_distance:
                        best_distance = distance
                        best = Match(rule=rule, keyword=keyword, score=1, fuzzy=True,
                                     corrected=f"{query_word} -> {keyword}", kind="typo")
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
