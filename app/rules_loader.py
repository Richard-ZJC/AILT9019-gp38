"""Load and validate data/rules.json and data/features.json.

Fail-fast policy: any missing field, empty source, unknown category or duplicate id
raises RulesError at start-up so the server never runs on half-broken data.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ACTIONS = ("RECYCLE", "REFUSE")

RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "rules.json"
FEATURES_PATH = Path(__file__).resolve().parent.parent / "data" / "features.json"


class RulesError(ValueError):
    """Raised when the rule set or the feature set is not usable."""


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    keywords: tuple[str, ...]
    parts: tuple[str, ...]
    materials: tuple[str, ...]
    category: str
    action: str
    instruction: str
    source_name: str
    source_url: str
    effective_date: str

    @property
    def source(self) -> dict[str, str]:
        return {"name": self.source_name, "url": self.source_url}

    @property
    def is_refusal(self) -> bool:
        return self.action == "REFUSE"

    def prompt_line(self) -> str:
        """Compact one-line description injected into the AI prompt."""
        parts = "/".join(self.parts) or "-"
        materials = "/".join(self.materials) or "-"
        return f"- {self.id} | parts: {parts} | materials: {materials} | category: {self.category} | action: {self.action}"

    def prompt_block(self) -> str:
        """Full rule text, used for the top-3 candidates in the prompt."""
        return (
            f"[{self.id}] {self.title}\n"
            f"  parts: {', '.join(self.parts) or '-'}\n"
            f"  materials: {', '.join(self.materials) or '-'}\n"
            f"  category: {self.category} ({self.action})\n"
            f"  handling: {self.instruction}\n"
            f"  source: {self.source_name} ({self.source_url}), effective {self.effective_date}"
        )


@dataclass(frozen=True)
class Feature:
    id: str
    label: str
    part: str
    material: str


@dataclass(frozen=True)
class FeatureSet:
    question: str
    options: tuple[Feature, ...] = field(default_factory=tuple)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(feature.id for feature in self.options)

    def by_id(self, feature_id: str) -> Feature | None:
        for feature in self.options:
            if feature.id == feature_id:
                return feature
        return None

    def labels(self) -> list[str]:
        return [feature.label for feature in self.options]


# ---------------------------------------------------------------- helpers


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RulesError(message)


def _as_str_list(value: Any, field_name: str, rule_id: str) -> tuple[str, ...]:
    _require(isinstance(value, list), f"{rule_id}: '{field_name}' must be a list")
    items: list[str] = []
    for item in value:  # type: ignore[union-attr]
        _require(isinstance(item, str) and item.strip(), f"{rule_id}: '{field_name}' entries must be non-empty strings")
        items.append(item.strip().lower())
    return tuple(items)


def _check_date(value: Any, rule_id: str) -> str:
    _require(isinstance(value, str) and value.strip(), f"{rule_id}: 'effective_date' missing")
    try:
        _dt.date.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - defensive
        raise RulesError(f"{rule_id}: 'effective_date' must be ISO date (YYYY-MM-DD)") from exc
    return value


# ---------------------------------------------------------------- loaders


def load_rules(path: str | Path | None = None) -> list[Rule]:
    """Load rules.json and validate every entry."""
    file_path = Path(path) if path else RULES_PATH
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RulesError(f"rules file not found: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise RulesError(f"rules file is not valid JSON: {exc}") from exc

    _require(isinstance(raw, dict), "rules.json root must be an object")
    categories = raw.get("categories")
    _require(isinstance(categories, list) and categories, "rules.json: 'categories' whitelist missing")
    whitelist = {str(item) for item in categories}

    entries = raw.get("rules")
    _require(isinstance(entries, list) and entries, "rules.json: 'rules' must be a non-empty list")
    _require(len(entries) >= 10, f"rules.json: expected 10-16 rules, found {len(entries)}")

    rules: list[Rule] = []
    seen: set[str] = set()

    for entry in entries:  # type: ignore[union-attr]
        _require(isinstance(entry, dict), "each rule must be an object")
        rule_id = entry.get("id")
        _require(isinstance(rule_id, str) and rule_id.strip(), "rule without a string 'id'")
        rule_id = rule_id.strip()
        _require(rule_id not in seen, f"duplicate rule id: {rule_id}")
        seen.add(rule_id)

        category = entry.get("category")
        _require(isinstance(category, str) and category in whitelist, f"{rule_id}: category '{category}' not in whitelist")

        action = entry.get("action")
        _require(isinstance(action, str) and action in ACTIONS, f"{rule_id}: action must be one of {ACTIONS}")

        source = entry.get("source")
        _require(isinstance(source, dict), f"{rule_id}: 'source' missing")
        source_name = source.get("name")
        source_url = source.get("url")
        _require(isinstance(source_name, str) and source_name.strip(), f"{rule_id}: source.name is empty")
        _require(isinstance(source_url, str) and source_url.startswith("http"), f"{rule_id}: source.url must be an http(s) link")

        instruction = entry.get("instruction")
        _require(isinstance(instruction, str) and instruction.strip(), f"{rule_id}: 'instruction' is required")

        keywords = _as_str_list(entry.get("keywords"), "keywords", rule_id)
        _require(bool(keywords), f"{rule_id}: 'keywords' must not be empty")

        rules.append(
            Rule(
                id=rule_id,
                title=str(entry.get("title") or rule_id),
                keywords=keywords,
                parts=_as_str_list(entry.get("parts", []), "parts", rule_id),
                materials=_as_str_list(entry.get("materials", []), "materials", rule_id),
                category=category,
                action=action,
                instruction=instruction.strip(),
                source_name=source_name.strip(),
                source_url=source_url.strip(),
                effective_date=_check_date(entry.get("effective_date"), rule_id),
            )
        )

    return rules


def load_features(path: str | Path | None = None) -> FeatureSet:
    """Load features.json - the only legal source of clarifying options."""
    file_path = Path(path) if path else FEATURES_PATH
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RulesError(f"features file not found: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise RulesError(f"features file is not valid JSON: {exc}") from exc

    _require(isinstance(raw, dict), "features.json root must be an object")
    question = raw.get("question")
    _require(isinstance(question, str) and question.strip(), "features.json: 'question' missing")

    entries = raw.get("options")
    _require(isinstance(entries, list) and entries, "features.json: 'options' must be a non-empty list")

    options: list[Feature] = []
    seen: set[str] = set()
    for entry in entries:  # type: ignore[union-attr]
        _require(isinstance(entry, dict), "each option must be an object")
        feature_id = entry.get("id")
        _require(isinstance(feature_id, str) and feature_id.strip(), "option without a string 'id'")
        feature_id = feature_id.strip()
        _require(feature_id not in seen, f"duplicate option id: {feature_id}")
        seen.add(feature_id)
        label = entry.get("label")
        _require(isinstance(label, str) and label.strip(), f"{feature_id}: 'label' missing")
        part = entry.get("part")
        material = entry.get("material")
        _require(isinstance(part, str) and part.strip(), f"{feature_id}: 'part' missing")
        _require(isinstance(material, str), f"{feature_id}: 'material' missing")
        options.append(Feature(id=feature_id, label=label.strip(), part=part.strip().lower(), material=material.strip().lower()))

    return FeatureSet(question=question.strip(), options=tuple(options))


def rule_ids(rules: Iterable[Rule]) -> set[str]:
    return {rule.id for rule in rules}
