"""The single adjudicator: every path ends here.

Nothing else in the codebase is allowed to produce a category. The AI may
*nominate* a rule id (`source_hint`) or describe components; this module
checks the nomination against rules.json and, if it is unknown, resolves the
components itself. No rule -> UNCOVERED -> the caller refuses. Never guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .matcher import normalise
from .rules_loader import Rule

UNCOVERED_COPY = "This packaging is not covered by the current rule set."


@dataclass(frozen=True)
class Verdict:
    part: str
    material: str
    status: str  # "MATCHED" | "UNCOVERED"
    category: str | None = None
    action: str | None = None
    rule_id: str | None = None
    rule_title: str | None = None
    instruction: str | None = None
    source: dict[str, str] | None = None
    effective_date: str | None = None
    matched_by: str | None = None
    bin: str | None = None

    @property
    def covered(self) -> bool:
        return self.status == "MATCHED"

    def to_dict(self) -> dict[str, Any]:
        if not self.covered:
            return {
                "part": self.part,
                "material": self.material,
                "status": "UNCOVERED",
                "category": None,
                "bin": None,
                "rule_id": None,
                "message": UNCOVERED_COPY,
            }
        return {
            "part": self.part,
            "material": self.material,
            "status": "MATCHED",
            "category": self.category,
            "bin": self.bin,
            "action": self.action,
            "rule_id": self.rule_id,
            "rule_title": self.rule_title,
            "instruction": self.instruction,
            "source": self.source,
            "effective_date": self.effective_date,
            "matched_by": self.matched_by,
        }


def _tokens(text: str) -> set[str]:
    words: set[str] = set()
    for word in normalise(text).split():
        words.add(word)
        if len(word) > 3 and word.endswith("s"):
            words.add(word[:-1])
    return words


_SPLIT = re.compile(r"[/,;|]+")


def _alts(value: str) -> list[str]:
    """Split 'cup/bowl' or 'plastic, foam' into separate candidates."""
    alternatives = [normalise(part) for part in _SPLIT.split(value or "")]
    return [alt for alt in alternatives if alt] or [""]


def _material_matches(rule: Rule, material: str) -> int:
    """2 = exact material list hit, 1 = partial (paper/paperboard), 0 = no."""
    if not material:
        return 0
    needle = normalise(material)
    best = 0
    for candidate in rule.materials:
        if needle == candidate:
            return 2
        if needle and candidate and (needle in candidate or candidate in needle):
            best = 1
    return best


def _keyword_score(rule: Rule, tokens: set[str]) -> int:
    best = 0
    for keyword in rule.keywords:
        keyword_tokens = _tokens(keyword)
        if keyword_tokens and keyword_tokens <= tokens:
            best = max(best, 2 * len(keyword_tokens))
    return best


class Adjudicator:
    def __init__(self, rules: Sequence[Rule]) -> None:
        self.rules = list(rules)
        self._index = {rule.id: rule for rule in self.rules}

    # ------------------------------------------------------------ helpers

    def rule(self, rule_id: str | None) -> Rule | None:
        if not rule_id:
            return None
        return self._index.get(rule_id)

    def _verdict_from(self, rule: Rule, part: str, material: str, matched_by: str) -> Verdict:
        return Verdict(
            part=part,
            material=material,
            status="MATCHED",
            category=rule.category,
            action=rule.action,
            rule_id=rule.id,
            rule_title=rule.title,
            instruction=rule.instruction,
            source=rule.source,
            effective_date=rule.effective_date,
            matched_by=matched_by,
            bin=rule.bin,
        )

    def from_rule(self, rule: Rule, part: str = "", material: str = "", matched_by: str = "keyword") -> Verdict:
        """Build a verdict straight from a rule that already won a zero-cost match."""
        return self._verdict_from(
            rule,
            normalise(part) or (rule.parts[0] if rule.parts else ""),
            normalise(material) or (rule.materials[0] if rule.materials else ""),
            matched_by,
        )

    # ------------------------------------------------------------ resolution

    def resolve(
        self,
        part: str,
        material: str = "",
        source_hint: str | None = None,
        query_text: str = "",
    ) -> Verdict:
        part = normalise(part)
        material = normalise(material)

        # 1. AI nomination, verified against rules.json
        hinted = self.rule(source_hint)
        if hinted is not None:
            return self._verdict_from(hinted, part, material, "source_hint")

        if not part and not material:
            return Verdict(part=part, material=material, status="UNCOVERED")

        tokens = _tokens(f"{part} {material} {query_text}")
        part_alts = _alts(part)
        material_alts = _alts(material)

        best_rule: Rule | None = None
        best_score = -1
        best_part = part
        best_material = material
        for rule in self.rules:
            if rule.is_refusal:
                continue
            matched_part = next((alt for alt in part_alts if alt and alt in rule.parts), "")
            material_score = max((_material_matches(rule, alt) for alt in material_alts), default=0)
            score = 0
            if matched_part:
                score += 4
            if material_score == 2:
                score += 3
            elif material_score == 1:
                score += 2
            score += _keyword_score(rule, tokens)
            if score > best_score:
                best_rule, best_score = rule, score
                best_part = matched_part or part
                best_material = material

        if best_rule is None or best_score < 4:
            return Verdict(part=part, material=material, status="UNCOVERED")

        matched_by = "part" if best_part else "keyword"
        return self._verdict_from(best_rule, best_part or part, best_material, matched_by)

    def resolve_all(
        self,
        components: Iterable[dict[str, Any]],
        source_hint: str | None = None,
        query_text: str = "",
    ) -> list[Verdict]:
        verdicts: list[Verdict] = []
        hint_used = False
        for component in components:
            hint = source_hint if not hint_used else None
            verdict = self.resolve(
                str(component.get("part") or ""),
                str(component.get("material") or ""),
                source_hint=hint,
                query_text=query_text,
            )
            hint_used = hint_used or (verdict.matched_by == "source_hint")
            verdicts.append(verdict)
        return verdicts

    def refusal_rule(self) -> Rule | None:
        for rule in self.rules:
            if rule.is_refusal:
                return rule
        return None
