"""Prompt assembly + response parsing (the only place that talks to the router).

One API call does identification *and* explanation: the prompt already carries
the candidate rule texts, and the model returns one sentence per rule id inside
the same JSON. That is the "merged call" from plan section 4.8.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .ai_plugin.router import Router, RouterResult
from .ai_plugin.schema import AIPayload, clean_products, fixed_clarify, validate
from .matcher import candidates as top_candidates
from .rules_loader import FeatureSet, Rule

SYSTEM_PROMPT = """You are the identification stage of "Campus Recycling Helper", a packaging sorter for the University of Hong Kong (HKU) campus.

Your job is NOT to decide a recycling category. Your job is to:
1. identify the product and each of its packaging components (part + material),
2. optionally nominate ONE rule id from the RULE INDEX below as source_hint,
3. ask a clarifying question when you cannot see the packaging,
4. write one short explanation sentence per nominated rule id.

Hard rules:
- The RULE INDEX is the complete universe. Never invent a rule, a category, a bin colour or a source.
- Never claim a source that is not in the RULE INDEX.
- If the item is clearly outside the scope of the rule set (hazardous waste, furniture, electronics, medical waste), set status to "REFUSE".
- Set status to "CLARIFY" only for a CONCRETE product (a named item with a recognisable container, or a specific product) where you cannot name a single packaging part, or the wording genuinely fits two different materials (e.g. "a bottle" could be plastic or glass). A bare category, brand or flavour word is BRAND, never CLARIFY.
- A product with several parts (cup + lid + sleeve) is NOT a reason to clarify: list every part with its own confidence and answer with status "OK".
- The user text may contain typos. If you can confidently map it to a product or packaging word (e.g. "Trea" -> Tetra Pak, "nissin cup noddle" -> cup noodles), silently correct it and answer OK with the corrected product name.
- If the text is NOT a plausible product or packaging mention at all (random letters, keyboard mash, a question or sentence about something unrelated), set status to "REFUSE" with "reason": "out_of_scope". Never ask clarifying questions about input you cannot recognise.
- "not_packaging" is ONLY for one specific, whole, countable physical item that the user names explicitly and that really exists in that form with no packaging: a single banana, an apple, a whole mango, loose potatoes. It is NEVER for a category, ingredient or bulk material (tea, coffee, milk, water, juice, rice, flour, sugar, salt, oil, soup, noodles, cereal) - those are sold in dozens of packagings, so they are BRAND. When you hesitate between "not_packaging" and BRAND, choose BRAND.
- Every REFUSE carries a "message" YOU write: 1-2 short sentences, in the SAME LANGUAGE as the user, explaining what you understood and why there is no recycling answer here, plus one concrete next step (e.g. describe the packaging instead). Grammar must be correct: sentence case, articles and singular/plural agreement, no capitalised common noun mid-sentence. Use the plural for a plural or uncountable item ("Loose tea leaves come without packaging"), the singular otherwise ("A whole banana comes without packaging"). The message explains - it never invents rules, bins or categories.
- If the text is ONLY a bare brand or company name, OR a bare flavour / product-line / category fragment with NO container word and NO size (e.g. "Coca-Cola", "Vitasoy", "Nestle", "维他", "lemon", "cola", "milk tea", "tea", "coffee", "rice"), set status to "BRAND", leave components empty and products null - the orchestrator fetches product candidates with web search and asks the user to pick one. A text that names a product line plus a size (e.g. "Coca-Cola Classic 330ml", "Vitasoy lemon tea 250ml") is a CONCRETE product: answer OK with its packaging components, never BRAND.
- clarify.options MUST be chosen from OPTION IDS and nothing else. Never invent an option.
- source_hint MUST be one of RULE IDS, or null.
- One component per packaging part. "part" and "material" are single lowercase words or short phrases - never alternatives such as "cup/bowl" or "plastic or paper". Split a product with several parts into several components.
- Reply with exactly one JSON object. No prose, no markdown fences.

JSON schema (all keys required):
{
  "status": "OK" | "CLARIFY" | "REFUSE" | "BRAND",
  "reason": "out_of_scope" | "not_packaging" (REFUSE only, otherwise omit),
  "message": "your own refusal message, required for REFUSE" (otherwise omit),
  "confidence": 0.0-1.0,
  "product": {"name": "string", "brand": "string"},
  "components": [{"part": "string", "material": "string", "confidence": 0.0-1.0}],
  "clarify": {"question": "string", "options": ["option-id"]} | null,
  "products": null,
  "explanations": {"rule-id": "one sentence on how to prepare this item"},
  "search_used": false,
  "source_hint": "rule-id" | null
}
"""


def _rule_index(rules: Sequence[Rule]) -> str:
    return "\n".join(rule.prompt_line() for rule in rules)


def build_messages(
    text: str,
    rules: Sequence[Rule],
    features: FeatureSet,
    correction: str | None = None,
) -> list[dict[str, str]]:
    candidates = top_candidates(text, rules, top_k=3)
    candidate_block = "\n\n".join(rule.prompt_block() for rule in candidates) or "(no keyword candidate - rely on the RULE INDEX)"

    system = (
        SYSTEM_PROMPT
        + "\nRULE IDS: " + ", ".join(rule.id for rule in rules)
        + "\nOPTION IDS: " + ", ".join(features.ids)
        + "\n\nRULE INDEX (id | parts | materials | category | action):\n" + _rule_index(rules)
        + "\n\nMOST LIKELY RULES FOR THIS QUERY (write one explanation sentence for each, keyed by rule id):\n"
        + candidate_block
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]
    if correction:
        messages.append({"role": "assistant", "content": "(previous answer was rejected)"})
        messages.append({"role": "user", "content": correction})
    return messages


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(raw: str) -> dict[str, Any] | None:
    """Best-effort extraction of a single JSON object from model output."""
    if not raw or not raw.strip():
        return None
    text = _FENCE.sub("", raw).strip()
    for candidate in (text,):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return value
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


@dataclass
class Identification:
    status: str  # "OK" (a payload came back) | "SERVICE_UNAVAILABLE"
    payload: AIPayload | None = None
    parse_attempts: int = 0
    model: str = ""
    attempts: list[Any] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.notes) or bool(self.payload and self.payload.degraded)


CORRECTION = (
    "Your previous reply was not valid JSON. Reply again with ONE JSON object only, "
    "matching the schema exactly, with no markdown fences and no commentary."
)

DISAMBIG_PROMPT = """You help "Campus Recycling Helper" narrow down a short query (a brand, a product
line, or a bare food/object word). Web search is ENABLED for this request - use
it. Do NOT assume the item is packaged: the user may be holding the bare item.

Candidates, most likely first (3-5 total):
1. If the query plausibly names an item that is commonly sold or found WITHOUT
   packaging (a whole fruit, a vegetable, loose tea leaves, loose coffee beans,
   plain rice), include it FIRST as
   {"name": "<the item> (no packaging)", "packaging": "none"} - e.g. "lemon"
   -> "Whole lemon (no packaging)". The user chooses this when they are not
   talking about a packaged product at all.
   NEVER do this for manufactured snacks and drinks (chocolate, biscuits,
   crisps, instant noodles, soft drinks): those always come in packaging, so
   listing one as "(no packaging)" would be false.
2. Then 2-4 CONCRETE packaged products matching the query that are actually
   sold in Hong Kong, each with its dominant packaging form as a short
   lowercase phrase such as "pet bottle", "tetra pak carton", "aluminium can".
Only include products a shopper would recognise as the same thing the query
names: no spirits, no unrelated categories, no invented brands.
Never invent a product that does not exist.

Reply with exactly one JSON object. No prose, no markdown fences:
{"products": [{"name": "Whole lemon (no packaging)", "packaging": "none"}]}
"""


class Identifier:
    def __init__(
        self,
        router: Router,
        rules: Sequence[Rule],
        features: FeatureSet,
        max_parse_retries: int = 1,
        search_router: Router | None = None,
    ) -> None:
        self.router = router
        self.rules = list(rules)
        self.features = features
        self.max_parse_retries = max_parse_retries
        # Optional web-search-enabled router, used only for brand disambiguation
        # so the per-query cost of enable_search stays surgical.
        self.search_router = search_router

    def run(self, text: str, image: str | None = None) -> Identification:
        messages = build_messages(text, self.rules, self.features)
        result: RouterResult = self.router.complete(messages, image=image)
        parse_attempts = 0
        notes: list[str] = []

        while True:
            if not result.ok:
                return Identification(status="SERVICE_UNAVAILABLE", attempts=result.attempts, model=result.raw_model)

            parse_attempts += 1
            parsed = parse_json(result.text)
            if parsed is not None:
                payload = validate(parsed, self.rules, self.features, raw_model=result.raw_model)
                notes.extend(payload.notes)
                return Identification(
                    status="OK",
                    payload=payload,
                    parse_attempts=parse_attempts,
                    model=result.raw_model,
                    attempts=result.attempts,
                    notes=notes,
                )

            notes.append(f"parse: attempt {parse_attempts} was not valid JSON")
            if parse_attempts > self.max_parse_retries:
                payload = AIPayload(
                    status="CLARIFY",
                    clarify=fixed_clarify(self.features),
                    raw_model=result.raw_model,
                    notes=["parse: no valid JSON after retry -> CLARIFY"],
                )
                return Identification(
                    status="OK",
                    payload=payload,
                    parse_attempts=parse_attempts,
                    model=result.raw_model,
                    attempts=result.attempts,
                    notes=notes,
                )

            messages = build_messages(text, self.rules, self.features, correction=CORRECTION)
            result = self.router.complete(messages, image=image)

    def disambiguate(self, brand_text: str) -> list[dict[str, str]]:
        """Web-search product candidates for a brand-only query. [] means degrade."""
        router = self.search_router or self.router
        messages = [
            {"role": "system", "content": DISAMBIG_PROMPT},
            {"role": "user", "content": brand_text},
        ]
        result = router.complete(messages)
        if not result.ok:
            return []
        parsed = parse_json(result.text)
        if parsed is None:
            return []
        notes: list[str] = []
        products = clean_products(parsed.get("products"), notes)
        # One usable candidate is enough: the pipeline guarantees the
        # "(no packaging)" escape hatch, so the user always has a choice.
        return products
