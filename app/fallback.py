"""Clarifying questions: fixed options only, no free text from the model by default.

This module is the SINGLE builder of clarify payloads. schema.py (AI path)
and pipeline.py (fallback path) both delegate here, so question/options/
labels/hints can never drift apart again.
"""

from __future__ import annotations

from typing import Any

from .rules_loader import FeatureSet


def build(features: FeatureSet, question: str | None = None, options: list[str] | None = None) -> dict[str, Any]:
    """Render a clarify payload. Every option id must exist in features.json."""
    allowed = list(features.ids)
    picked = [option for option in (options or allowed) if option in allowed] or allowed
    labels = {feature.id: feature.label for feature in features.options}
    hints = {feature.id: feature.hint for feature in features.options}
    return {
        "question": question or features.question,
        "options": picked,
        "labels": {option: labels[option] for option in picked},
        "hints": {option: hints[option] for option in picked},
        "source": "fixed",
    }


def component_from_choice(choice: str, features: FeatureSet) -> dict[str, Any] | None:
    """Map a chosen option id to a component description for the adjudicator."""
    feature = features.by_id(choice)
    if feature is None:
        return None
    return {"part": feature.part, "material": feature.material, "confidence": 1.0}
