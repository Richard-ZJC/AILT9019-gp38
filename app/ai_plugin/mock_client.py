"""Deterministic offline stand-in for a real model.

Used by the test suite, by `python server.py --mock` and by demos on a machine
that has no API key. It reads rules.json the same way the adjudicator does, so
its output still has to survive the real schema validation and the real
matching step - it is a stand-in for the *model*, not a shortcut around the
pipeline. When a real key is configured this client is never constructed.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Sequence

from ..matcher import match as keyword_match
from ..rules_loader import FeatureSet, Rule

PACKAGING_WORDS = ("bottle", "cup", "can", "box", "bag", "carton", "lid", "tray", "jar", "wrapper", "packaging")


class MockClient:
    name = "mock"
    supports_images = False

    def __init__(self, rules: Sequence[Rule], features: FeatureSet, delay: float = 0.0) -> None:
        self.rules = list(rules)
        self.features = features
        self.delay = delay

    def _extract_text(self, messages: Sequence[dict[str, Any]]) -> str:
        for message in reversed(list(messages)):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    def complete(self, messages: Sequence[dict[str, Any]], image: str | None = None, stream: bool = False):
        if stream:
            return self.stream(messages, image=image)
        if self.delay:
            import time

            time.sleep(self.delay)

        text = self._extract_text(messages)
        lowered = text.lower()

        hit = keyword_match(text, self.rules)
        if hit is not None:
            rule = hit.rule
            payload = {
                "status": "REFUSE" if rule.is_refusal else "OK",
                "confidence": 0.9,
                "product": {"name": text.strip()[:80], "brand": ""},
                "components": [
                    {
                        "part": rule.parts[0] if rule.parts else "packaging",
                        "material": rule.materials[0] if rule.materials else "",
                        "confidence": 0.9,
                    }
                ],
                "clarify": None,
                "explanations": {rule.id: rule.instruction},
                "search_used": False,
                "source_hint": rule.id,
                "raw_model": "mock",
            }
            if rule.is_refusal:
                payload["components"] = []
            return json.dumps(payload)

        part = next((word for word in PACKAGING_WORDS if word in lowered), "")
        if part:
            payload = {
                "status": "OK",
                "confidence": 0.45,
                "product": {"name": text.strip()[:80], "brand": ""},
                "components": [{"part": part, "material": "", "confidence": 0.45}],
                "clarify": None,
                "explanations": {},
                "search_used": False,
                "source_hint": None,
                "raw_model": "mock",
            }
            return json.dumps(payload)

        payload = {
            "status": "CLARIFY",
            "confidence": 0.15,
            "product": {"name": text.strip()[:80], "brand": ""},
            "components": [],
            "clarify": {"question": self.features.question, "options": list(self.features.ids[:6])},
            "explanations": {},
            "search_used": False,
            "source_hint": None,
            "raw_model": "mock",
        }
        return json.dumps(payload)

    def stream(self, messages: Sequence[dict[str, Any]], image: str | None = None) -> Iterator[str]:
        yield self.complete(messages, image=image)
