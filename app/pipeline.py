"""Orchestration: zero-cost layer -> AI layer -> single adjudicator -> output.

Order matters. The cheap local match runs first and, when it hits, no API call is
made at all. Anything that reaches the AI still has to pass through match.py
before a category exists. Every branch ends in exactly one of four statuses.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from . import explain, fallback, matcher, refusal
from .ai_plugin.router import Router
from .identify import Identification, Identifier
from .match import Adjudicator, Verdict
from .memory import Memory
from .rules_loader import FeatureSet, Rule
from .session import Session, SessionStore

MIN_CONFIDENCE = 0.5

# Ingredients and bulk materials are sold in dozens of packagings, so a bare
# mention of one is never "an item without packaging" - it is a query that
# needs disambiguation. Kept small and explicit: only words whose own name
# denotes the loose substance, not a product.
_BULK_MATERIALS = frozenset(
    {
        "tea", "coffee", "milk", "water", "juice", "rice", "flour", "sugar",
        "salt", "oil", "soup", "porridge", "cereal", "grain", "powder",
        "sauce", "honey", "yogurt", "yoghurt", "noodle", "noodles",
    }
)
_BULK_PHRASES = ("茶", "咖啡", "牛奶", "果汁", "大米", "面粉", "白糖", "食用油")
_CONTAINER_WORDS = (
    "bag", "cup", "bottle", "can", "box", "carton", "tin", "jar", "pack",
    "packet", "wrapper", "sleeve", "lid", "tube", "pouch", "sachet",
)


def _is_bare_noun(text: str) -> bool:
    """A short noun phrase with no size and no container word.

    Such a query says nothing about packaging, so "which packaging do you see
    on the product?" is unanswerable - the user may not even be holding one.
    """
    lowered = " ".join((text or "").lower().replace("-", " ").split())
    words = re.findall(r"[a-z]+", lowered)
    if not words or len(words) > 2 or any(character.isdigit() for character in lowered):
        return False
    if any(word in _CONTAINER_WORDS for word in words):
        return False
    return True


def _is_bulk_material(text: str) -> bool:
    """True when the query names a substance rather than one unpackaged object."""
    lowered = " ".join((text or "").lower().replace("-", " ").split())
    if not lowered:
        return False
    words = re.findall(r"[a-z]+", lowered)
    if any(word in _CONTAINER_WORDS for word in words):
        return False
    if words and any(word in _BULK_MATERIALS for word in words):
        return True
    return any(phrase in lowered for phrase in _BULK_PHRASES)



class Pipeline:
    def __init__(
        self,
        rules: Sequence[Rule],
        features: FeatureSet,
        router: Router | None = None,
        sessions: SessionStore | None = None,
        memory: Memory | None = None,
        min_confidence: float = MIN_CONFIDENCE,
        debug: bool = False,
        use_fast_path: bool = True,
        search_router: Router | None = None,
    ) -> None:
        self.rules = list(rules)
        self.features = features
        self.adjudicator = Adjudicator(self.rules)
        self.router = router
        self.identifier = Identifier(router, self.rules, features) if router is not None else None
        if self.identifier is not None and search_router is not None:
            self.identifier.search_router = search_router
        self.sessions = sessions or SessionStore()
        self.memory = memory or Memory()
        self.min_confidence = min_confidence
        self.debug = debug
        # Test switch: False forces every query through the AI path, so the model
        # can be evaluated on items the keyword layer would otherwise answer.
        self.use_fast_path = use_fast_path

    # ------------------------------------------------------------ entry points

    def identify(self, text: str, session_id: str | None = None) -> dict[str, Any]:
        query = (text or "").strip()
        if not query:
            raise ValueError("query text is empty")

        session = self.sessions.get_or_create(session_id)
        session.add_turn("user", query)
        session.remember(last_text=query)

        # 0a. local hazard net - never reaches the model
        hazard = refusal.hazard_hit(query)
        if hazard:
            return self._refusal(session, refusal.payload("hazard", f"local hazard keyword: {hazard}"))

        # 0a'. the user picked the unpackaged candidate from a disambiguation
        # list - deterministic, no model call, and it beats everything below
        # except hazards ('no packaging: syringe' must still refuse as hazard).
        if query.strip().lower().startswith("no packaging:"):
            item = query.strip()[len("no packaging:") :].strip() or "this item"
            return self._refusal(session, refusal.not_packaging_item(item))

        # 0b. exact keyword match - zero API calls
        hit = matcher.match(query, self.rules) if self.use_fast_path else None
        # 0c. typo rescue - still zero API calls ('trea' -> 'tetra pak')
        if hit is None and self.use_fast_path:
            hit = matcher.fuzzy_match(query, self.rules)
        if hit is not None:
            if hit.rule.is_refusal:
                return self._refusal(session, refusal.payload("out_of_scope", f"keyword: {hit.rule.id}"))
            verdict = self.adjudicator.from_rule(hit.rule, matched_by="keyword")
            return self._ok(
                session,
                text=query,
                verdicts=[verdict],
                confidence=0.9 if hit.fuzzy else 1.0,
                mode="fuzzy" if hit.fuzzy else "exact",
                model="none",
                explanations={},
                product={"name": query, "brand": ""},
                notes=[f"loose match: {hit.corrected}"] if hit.fuzzy else (),
            )

        # 1. AI identification
        if self.identifier is None:
            return self._unavailable(session, reason="no AI provider configured")

        identification = self.identifier.run(query)
        return self._from_ai(session, query, identification)

    def answer(self, session_id: str, choice: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            raise LookupError("unknown session_id")

        component = fallback.component_from_choice(choice, self.features)
        if component is None:
            raise ValueError(f"choice '{choice}' is not in features.json")

        if session.pending is None and not session.answers():
            raise ValueError("this session has no pending question")

        session.add_turn("user", f"(choice) {choice}")
        session.add_answer(choice)
        self.sessions.clear_pending(session)

        verdict = self.adjudicator.resolve(
            component["part"],
            component["material"],
            query_text=str(session.context.get("last_text") or ""),
        )
        if not verdict.covered:
            return self._refusal(session, refusal.payload("out_of_scope", f"answer '{choice}' matched no rule"))

        return self._ok(
            session,
            text=str(session.context.get("last_text") or ""),
            verdicts=[verdict],
            confidence=0.95,
            mode="clarify_answer",
            model="none",
            explanations={},
            product=session.context.get("product") or {"name": "", "brand": ""},
        )

    # ------------------------------------------------------------ AI branch

    def _from_ai(self, session: Session, query: str, identification: Identification) -> dict[str, Any]:
        if identification.status != "OK" or identification.payload is None:
            return self._unavailable(
                session,
                reason="all configured providers failed",
                attempts=identification.attempts,
            )

        payload = identification.payload

        if payload.status == "SERVICE_UNAVAILABLE":
            return self._unavailable(session, reason="provider reported SERVICE_UNAVAILABLE")

        if payload.status == "REFUSE":
            kind = payload.reason or "out_of_scope"
            if kind == "not_packaging" and _is_bulk_material(query):
                # Deterministic guard on top of the prompt: "tea" is sold in
                # tins, bottles, cartons and tea bags, so refusing it as "an
                # item without packaging" is an over-assumption. Send it to the
                # product search instead - the search list still offers a
                # "(no packaging)" choice for loose tea.
                return self._disambiguate(session, query, payload, identification)
            body = refusal.payload(kind, f"model: {kind}")
            # The model's own message wins (it follows the user's language);
            # the fixed refusal.py copy is the fallback, never a second voice.
            if payload.message:
                body["message"] = payload.message
            return self._refusal(session, body)

        if payload.status == "BRAND":
            return self._disambiguate(session, query, payload, identification)

        if payload.status == "CLARIFY" and _is_bare_noun(query):
            # "lemon", "milk tea", "chocolate": no container, no size, nothing
            # the user could look at. Asking which packaging they see is a dead
            # end, so the model's CLARIFY is replaced by the product search.
            return self._disambiguate(session, query, payload, identification)

        if payload.status == "CLARIFY" or self._low_confidence(payload):
            # payload.clarify already comes from fallback.build (schema delegates),
            # so question/options/labels/hints/source are always consistent here.
            clarify = payload.clarify or fallback.build(self.features)
            return self._ask(session, clarify, payload, identification)

        verdicts = self.adjudicator.resolve_all(payload.components, payload.source_hint, query_text=query)
        covered = [verdict for verdict in verdicts if verdict.covered]

        if not covered:
            return self._refusal(
                session,
                refusal.payload("out_of_scope", "model components matched no rule"),
                raw_model=identification.model,
            )
        if any(verdict.action == "REFUSE" for verdict in covered):
            return self._refusal(
                session,
                refusal.payload("out_of_scope", "model nominated an out-of-scope rule"),
                raw_model=identification.model,
            )

        confidence = min(payload.confidence or 0.0, 1.0)
        if confidence <= 0:
            confidence = max((component.get("confidence", 0.0) for component in payload.components), default=0.0)

        return self._ok(
            session,
            text=query,
            verdicts=verdicts,
            confidence=confidence,
            mode="ai",
            model=identification.model,
            explanations=payload.explanations,
            product=payload.product or {"name": query, "brand": ""},
            search_used=payload.search_used,
            source_hint=payload.source_hint,
            degraded=identification.degraded,
            notes=identification.notes,
        )

    def _low_confidence(self, payload) -> bool:
        if not payload.components:
            return True
        best = max(component.get("confidence", 0.0) for component in payload.components)
        return best < self.min_confidence

    def _disambiguate(self, session: Session, query: str, payload, identification: Identification) -> dict[str, Any]:
        """Brand-only query: fetch product candidates via web search, let the user pick."""
        if session.context.get("disambiguated") == query:
            # Loop guard, keyed on the query itself: the same words came back a
            # second time, so searching again would repeat the same list. A
            # different query in the same session is not a loop.
            return self._ask(session, fallback.build(self.features), payload, identification)
        products = payload.products
        if not products and self.identifier is not None:
            products = self.identifier.disambiguate(query)
        products = self._with_unpacked_option(query, products)
        question = f"Which '{query}' do you have?"
        clarify = fallback.build_products(question, products)
        session.remember(disambiguated=query)
        return self._ask(session, clarify, payload, identification, search_used=True)

    def _with_unpacked_option(self, query: str, products: list[dict[str, str]]) -> list[dict[str, str]]:
        """Keep the '(no packaging)' escape hatch first when search offered one.

        It is not synthesised for every query: pretending that chocolate or cola
        might come unpackaged would be as wrong as assuming lemon always does.
        When search returns nothing at all we still need one usable option, and
        "no packaging" is the only honest one.
        """
        if not products:
            return [{"name": f"{query} (no packaging)", "packaging": "none"}]
        unique: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in products:
            key = " ".join(item.get("name", "").lower().split())
            if key and key not in seen:
                seen.add(key)
                unique.append(item)
        products = unique
        unpacked = [item for item in products if (item.get("packaging") or "").lower() == "none"]
        rest = [item for item in products if (item.get("packaging") or "").lower() != "none"]
        if unpacked:
            # A search that lists "KitKat Tablet" and "KitKat Tablet (no
            # packaging)" contradicts itself: the same product cannot be both.
            # The packaged entry wins, and the phantom twin is dropped.
            base = unpacked[0]["name"].lower().replace(" (no packaging)", "").strip()
            if not any(base in item.get("name", "").lower() for item in rest):
                return (unpacked[:1] + rest)[:5]
            return rest[:5]
        return products[:5]

    # ------------------------------------------------------------ response builders

    def _base_body(self, session: Session, status: str, mode: str) -> dict[str, Any]:
        """The one response envelope. Every branch starts here and overrides."""
        return {
            "status": status,
            "session_id": session.id,
            "confidence": 0.0,
            "product": {},
            "components": [],
            "clarify": None,
            "message": "",
            "search_used": False,
            "source_hint": None,
            "raw_model": "none",
            "mode": mode,
            "degraded": False,
        }

    def _ask(
        self,
        session: Session,
        clarify: dict[str, Any],
        payload,
        identification: Identification,
        search_used: bool = False,
    ) -> dict[str, Any]:
        if not self.sessions.can_ask(session):
            # Two questions is the limit. Rather than punt ("I still cannot
            # tell - check it yourself"), spend one more model call that is
            # forbidden to ask again and must commit to the most likely answer.
            return self._commit(session, clarify, identification)

        self.sessions.note_question(session, clarify)
        session.add_turn("assistant", clarify.get("question", ""))
        body = self._base_body(session, "CLARIFY", "clarify")
        body.update(
            {
                "confidence": payload.confidence if payload else 0.0,
                "product": payload.product if payload else {},
                "clarify": clarify,
                "search_used": bool(search_used) or clarify.get("source") == "ai-search",
                "raw_model": identification.model,
                "degraded": identification.degraded,
            }
        )
        return self._with_diagnostics(body, identification.attempts)

    def _history_text(self, session: Session) -> str:
        lines = []
        for turn in session.turns[-8:]:
            who = "User" if turn.role == "user" else "Helper"
            lines.append(f"{who}: {turn.text}")
        return "\n".join(lines)

    def _commit(self, session: Session, clarify: dict[str, Any], identification: Identification) -> dict[str, Any]:
        """Question budget spent: turn the best available signal into an answer.

        "I still cannot tell, check it yourself" is not an answer. One final
        model call is allowed, and it is forbidden to ask again: it must pick the
        most defensible packaging (or a genuine no-packaging/out-of-scope call).
        """
        query = str(session.context.get("last_text") or "")

        final = self.identifier.commit(query, self._history_text(session)) if self.identifier is not None else None
        if final is not None and final.status == "OK" and final.payload is not None:
            decided = final.payload
            if decided.status == "REFUSE":
                kind = decided.reason or "out_of_scope"
                body = refusal.payload(kind, f"model commit: {kind}")
                if decided.message:
                    body["message"] = decided.message
                return self._refusal(session, body, raw_model=final.model)
            covered = [
                verdict
                for verdict in self.adjudicator.resolve_all(
                    decided.components or [], decided.source_hint, query_text=query
                )
                if verdict.covered
            ]
            if covered:
                return self._ok(
                    session,
                    text=query,
                    verdicts=covered,
                    confidence=min(decided.confidence or 0.4, 0.6),
                    mode="best_guess",
                    model=final.model,
                    explanations=decided.explanations,
                    product=decided.product or {"name": query, "brand": ""},
                    degraded=final.degraded,
                    notes=["Best guess after two questions - check the packaging before you sort."],
                )

        # Deterministic backstop: the leading clarify option, when it is a real part.
        for option in (clarify.get("options") or [])[:1]:
            component = fallback.component_from_choice(option, self.features)
            if component is None:
                continue
            verdict = self.adjudicator.resolve(component["part"], component["material"], query_text=query)
            if verdict.covered:
                return self._ok(
                    session,
                    text=query,
                    verdicts=[verdict],
                    confidence=0.35,
                    mode="best_guess",
                    model="none",
                    explanations={},
                    product=session.context.get("product") or {"name": query, "brand": ""},
                    notes=["Best guess from the closest option - check the packaging before you sort."],
                )

        # No usable signal. The card still owes the user a move, not a shrug:
        # staff at the re Kiosk sort by hand, and one container word gets us
        # unstuck. This is the only path that ends outside OK, and it is rare.
        body = refusal.payload("out_of_scope", "best_effort: no usable signal")
        body["message"] = (
            "I could not pin this down to one packaging type, so here is the safe move: "
            "rinse the item and hand it in at the re Kiosk, where staff sort it by hand. "
            "Tell me the container itself - bottle, cup, carton or can - and I will give you the exact slot."
        )
        return self._refusal(session, body, raw_model=(final.model if final else "none"))

    def _ok(
        self,
        session: Session,
        *,
        text: str,
        verdicts: Sequence[Verdict],
        confidence: float,
        mode: str,
        model: str,
        explanations: dict[str, str],
        product: dict[str, Any],
        search_used: bool = False,
        source_hint: str | None = None,
        degraded: bool = False,
        notes: Sequence[str] = (),
    ) -> dict[str, Any]:
        components: list[dict[str, Any]] = []
        for verdict in verdicts:
            item = verdict.to_dict()
            if verdict.covered:
                item["explanation"] = explain.compose(verdict, explanations)
                item["source_line"] = explain.source_line(verdict)
            components.append(item)

        session.add_turn("assistant", f"OK: {', '.join(item.get('category') or '' for item in components)}")
        session.remember(product=product, components=components)
        self.memory.record({"session_id": session.id, "text": text, "components": components})

        body = self._base_body(session, "OK", mode)
        body.update(
            {
                "confidence": round(float(confidence), 3),
                "product": product,
                "components": components,
                "search_used": bool(search_used),
                "source_hint": source_hint,
                "raw_model": model,
                "degraded": bool(degraded),
                "notes": list(notes),
            }
        )
        return body

    def _refusal(self, session: Session, refusal_body: dict[str, Any], raw_model: str = "none") -> dict[str, Any]:
        session.add_turn("assistant", refusal_body.get("message", ""))
        body = self._base_body(session, refusal_body.get("status", "REFUSE"), "refuse")
        body.update(
            {
                "product": session.context.get("product") or {},
                "message": refusal_body.get("message", refusal.OUT_OF_SCOPE),
                "reason": refusal_body.get("reason", ""),
                "raw_model": raw_model,
            }
        )
        self.memory.record({"session_id": session.id, "refused": body["reason"]})
        return body

    def _unavailable(self, session: Session, reason: str, attempts: Sequence[Any] = ()) -> dict[str, Any]:
        # refusal.payload("service_unavailable") already sets the right status and
        # message; the only key it cannot know is mode.
        body = self._refusal(session, refusal.payload("service_unavailable", reason))
        body["mode"] = "service_unavailable"
        return self._with_diagnostics(body, attempts)

    def _with_diagnostics(self, body: dict[str, Any], attempts: Sequence[Any]) -> dict[str, Any]:
        if not self.debug:
            return body
        body["diagnostics"] = {
            "attempts": [
                {
                    "provider": getattr(attempt, "provider", ""),
                    "outcome": getattr(attempt, "outcome", ""),
                    "elapsed": round(float(getattr(attempt, "elapsed", 0.0)), 3),
                    "detail": getattr(attempt, "detail", ""),
                }
                for attempt in attempts
            ],
            "providers": self.router.provider_names() if self.router else [],
        }
        return body
