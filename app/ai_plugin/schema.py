"""Strict response template + schema validation.

The AI is allowed to be wrong; it is not allowed to be unstructured. Anything
that fails a check is either repaired (source_hint -> null), replaced with the
fixed option list, or downgraded to CLARIFY. A malformed answer never becomes
a recycling category on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..fallback import build as build_clarify
from ..rules_loader import FeatureSet, Rule

STATUSES = ("OK", "CLARIFY", "REFUSE", "SERVICE_UNAVAILABLE", "BRAND")

TEMPLATE: dict[str, Any] = {
    "status": "OK | CLARIFY | REFUSE | SERVICE_UNAVAILABLE",
    "reason": "out_of_scope | not_packaging (REFUSE only)",
    "confidence": 0.0,
    "product": {"name": "Vitasoy lemon tea", "brand": "Vitasoy"},
    "components": [{"part": "carton", "material": "tetra pak", "confidence": 0.9}],
    "clarify": {"question": "Which packaging do you see?", "options": ["bottle", "cup", "box"]},
    "products": None,
    "explanations": {"beverage-carton": "Empty and rinse, then flatten into the Tetra Pak slot."},
    "search_used": False,
    "source_hint": None,
    "raw_model": "openai | anthropic",
}


@dataclass
class AIPayload:
    status: str
    confidence: float = 0.0
    product: dict[str, str] = field(default_factory=dict)
    components: list[dict[str, Any]] = field(default_factory=list)
    clarify: dict[str, Any] | None = None
    products: list[dict[str, str]] = field(default_factory=list)
    explanations: dict[str, str] = field(default_factory=dict)
    search_used: bool = False
    source_hint: str | None = None
    reason: str = ""
    raw_model: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.notes)


def fixed_clarify(features: FeatureSet, question: str | None = None) -> dict[str, Any]:
    """Thin alias - fallback.build is the single clarify builder."""
    return build_clarify(features, question)


def _as_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def clean_products(value: Any, notes: list[str] | None = None) -> list[dict[str, str]]:
    """Validate an AI-proposed product candidate list ({name, packaging})."""
    notes = notes if notes is not None else []
    if not isinstance(value, list):
        if value is not None:
            notes.append("products: not a list")
        return []
    cleaned: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            notes.append("products: non-object entry dropped")
            continue
        name = item.get("name")
        packaging = item.get("packaging")
        if not isinstance(name, str) or not name.strip():
            notes.append("products: entry without 'name' dropped")
            continue
        cleaned.append(
            {
                "name": name.strip(),
                "packaging": packaging.strip() if isinstance(packaging, str) else "",
            }
        )
    return cleaned[:5]


def _clean_components(value: Any, notes: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        notes.append("components: not a list")
        return []
    cleaned: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            notes.append("components: non-object entry dropped")
            continue
        part = item.get("part")
        material = item.get("material")
        if not isinstance(part, str) or not part.strip():
            notes.append("components: entry without 'part' dropped")
            continue
        cleaned.append(
            {
                "part": part.strip().lower(),
                "material": material.strip().lower() if isinstance(material, str) else "",
                "confidence": _as_confidence(item.get("confidence", 0.0)),
            }
        )
    return cleaned


def _clean_clarify(
    value: Any,
    features: FeatureSet,
    notes: list[str],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    question = value.get("question")
    options = value.get("options")
    if not isinstance(options, list) or not options:
        notes.append("clarify.options: missing")
        return None
    allowed = set(features.ids)
    picked: list[str] = []
    for option in options:
        if isinstance(option, str) and option.strip().lower() in allowed:
            picked.append(option.strip().lower())
        else:
            notes.append(f"clarify.options: '{option}' is outside features.json")
    if not picked or len(picked) != len(options):
        notes.append("clarify.options: replaced with the fixed option list")
        return fixed_clarify(features, question if isinstance(question, str) and question.strip() else None)
    if len(picked) < 2:
        # A question with a single answer is not a question - show the full list.
        notes.append("clarify.options: fewer than 2 choices -> fixed option list")
        return fixed_clarify(features, question if isinstance(question, str) and question.strip() else None)
    clean_question = question.strip() if isinstance(question, str) and question.strip() else None
    return build_clarify(features, clean_question, picked)


def _clean_explanations(value: Any, notes: list[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        if value is not None:
            notes.append("explanations: not an object")
        return {}
    cleaned: dict[str, str] = {}
    for key, text in value.items():
        if isinstance(key, str) and isinstance(text, str) and text.strip():
            cleaned[key.strip()] = text.strip()
        else:
            notes.append("explanations: non-string entry dropped")
    return cleaned


def validate(
    raw: Any,
    rules: Sequence[Rule],
    features: FeatureSet,
    raw_model: str = "",
) -> AIPayload:
    """Turn an untrusted dict into an AIPayload, or downgrade to CLARIFY."""
    notes: list[str] = []
    if not isinstance(raw, dict):
        raw = {}
        notes.append("root: not a JSON object")

    status = raw.get("status")
    if status not in STATUSES:
        notes.append(f"status: '{status}' is not one of {STATUSES}")
        status = "CLARIFY"

    product_raw = raw.get("product")
    product: dict[str, str] = {}
    if isinstance(product_raw, dict):
        name = product_raw.get("name")
        brand = product_raw.get("brand")
        if isinstance(name, str) and name.strip():
            product["name"] = name.strip()
        if isinstance(brand, str) and brand.strip():
            product["brand"] = brand.strip()

    components = _clean_components(raw.get("components"), notes)
    clarify = _clean_clarify(raw.get("clarify"), features, notes)
    products = clean_products(raw.get("products"), notes)
    explanations = _clean_explanations(raw.get("explanations"), notes)

    source_hint = raw.get("source_hint")
    known_ids = {rule.id for rule in rules}
    if source_hint is not None:
        if isinstance(source_hint, str) and source_hint.strip() in known_ids:
            source_hint = source_hint.strip()
        else:
            notes.append(f"source_hint: '{source_hint}' is not a rule id -> null")
            source_hint = None

    if status == "OK" and not components:
        notes.append("status: OK without components -> CLARIFY")
        status = "CLARIFY"

    # The only model-visible refusal nuance: a real item that simply has no
    # packaging. Everything else the model calls "REFUSE" stays out_of_scope.
    reason = raw.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if reason and reason not in ("out_of_scope", "not_packaging"):
        notes.append(f"reason: '{reason}' is not a known refusal reason -> dropped")
        reason = ""

    # BRAND passes through even with empty products: pipeline._disambiguate owns
    # the degrade decision (it can still fetch candidates via web search).

    if status == "CLARIFY" and clarify is None:
        clarify = fixed_clarify(features)
        notes.append("clarify: filled from the fixed option list")

    return AIPayload(
        status=status,
        confidence=_as_confidence(raw.get("confidence", 0.0)),
        product=product,
        components=components,
        clarify=clarify,
        products=products,
        explanations=explanations,
        search_used=bool(raw.get("search_used", False)),
        source_hint=source_hint,
        reason=reason,
        raw_model=raw_model or str(raw.get("raw_model") or ""),
        notes=notes,
    )
