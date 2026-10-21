"""Explanation text for a matched component.

The AI's sentence is used only when it is attached to the rule that actually
won. Otherwise the rule's own instruction plus its source is shown - never a
second API call, never invented handling advice.
"""

from __future__ import annotations

from typing import Mapping

from .match import Verdict


def compose(verdict: Verdict, explanations: Mapping[str, str]) -> str:
    if not verdict.covered:
        return ""

    for key in (verdict.rule_id or "", verdict.part or ""):
        text = explanations.get(key)
        if text and text.strip():
            return text.strip()

    if verdict.instruction:
        return verdict.instruction
    return f"Follow the campus rule {verdict.rule_id}."


def source_line(verdict: Verdict) -> str:
    if not verdict.covered or not verdict.source:
        return ""
    return f"{verdict.source.get('name', '')} ({verdict.effective_date})"
