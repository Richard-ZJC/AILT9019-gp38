#!/usr/bin/env python3
"""AI plugin tests - routing, schema, degradation, session budget.

All providers here are fakes: no network, no key. Run with
    python tests/test_ai_plugin.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import refusal
from app.ai_plugin.mock_client import MockClient
from app.ai_plugin.router import Router
from app.ai_plugin.schema import validate
from app.identify import Identifier
from app.pipeline import Pipeline
from app.rules_loader import load_features, load_rules
from app.session import SessionStore

RULES = load_rules()
FEATURES = load_features()

RESULTS: list[tuple[str, bool, str]] = []


class FakeClient:
    """Scripted provider. `replies` is consumed call by call."""

    def __init__(self, name: str, replies: Sequence[Any], delay: float = 0.0) -> None:
        self.name = name
        self.replies = list(replies)
        self.delay = delay
        self.calls = 0

    def complete(self, messages, image=None, stream=False):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        reply = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return reply


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" - {detail}" if detail and not condition else ""))


def good_payload(**overrides: Any) -> str:
    payload = {
        "status": "OK",
        "confidence": 0.93,
        "product": {"name": "Vitasoy lemon tea", "brand": "Vitasoy"},
        "components": [{"part": "carton", "material": "tetra pak", "confidence": 0.93}],
        "clarify": None,
        "explanations": {"beverage-carton": "Rinse and flatten, then the Tetra Pak slot."},
        "search_used": False,
        "source_hint": "beverage-carton",
    }
    payload.update(overrides)
    return json.dumps(payload)


# ---------------------------------------------------------------- 1. schema

def test_schema_accepts_valid() -> None:
    payload = validate(json.loads(good_payload()), RULES, FEATURES, raw_model="openai")
    ok = (
        payload.status == "OK"
        and payload.components[0]["part"] == "carton"
        and payload.source_hint == "beverage-carton"
        and not payload.notes
    )
    check("1 schema: valid payload passes untouched", ok, str(payload.notes))


# ---------------------------------------------------------------- 2. failover

def test_failover_to_anthropic() -> None:
    slow = FakeClient("openai", [good_payload()], delay=0.5)
    backup = FakeClient("anthropic", [good_payload()])
    router = Router([slow, backup], timeout=0.15)
    result = router.complete([{"role": "user", "content": "Vitasoy lemon tea"}])
    ok = (
        result.ok
        and result.raw_model == "anthropic"
        and result.attempts[0].outcome == "timeout"
        and result.attempts[1].outcome == "ok"
        and slow.calls == 1
    )
    check("2 router: openai timeout -> anthropic answers", ok, result.summary())


# ---------------------------------------------------------------- 3. both down

def test_both_timeout() -> None:
    first = FakeClient("openai", [good_payload()], delay=0.5)
    second = FakeClient("anthropic", [good_payload()], delay=0.5)
    router = Router([first, second], timeout=0.15)
    result = router.complete([{"role": "user", "content": "Vitasoy lemon tea"}])
    ok = result.status == "SERVICE_UNAVAILABLE" and all(a.outcome == "timeout" for a in result.attempts)
    check("3 router: both channels time out -> SERVICE_UNAVAILABLE", ok, result.summary())


# ---------------------------------------------------------------- 4. parse retry

def test_parse_failure_downgrades() -> None:
    client = FakeClient("openai", ["I think this is a carton, sorry.", "still not json"])
    identifier = Identifier(Router([client], timeout=5), RULES, FEATURES)
    result = identifier.run("Vitasoy lemon tea")
    ok = (
        result.status == "OK"
        and result.payload is not None
        and result.payload.status == "CLARIFY"
        and result.parse_attempts == 2
        and client.calls == 2
    )
    check("4 identify: unparsable -> one retry -> CLARIFY", ok, f"parse_attempts={result.parse_attempts} calls={client.calls}")


# ---------------------------------------------------------------- 5. no retry on timeout

def test_timeout_no_retry() -> None:
    client = FakeClient("openai", [good_payload()], delay=0.6)
    router = Router([client], timeout=0.15)
    router.complete([{"role": "user", "content": "anything"}])
    check("5 router: a timed-out provider is called exactly once", client.calls == 1, f"calls={client.calls}")


# ---------------------------------------------------------------- 6. bad source_hint

def test_invalid_source_hint_nulled() -> None:
    raw = json.loads(good_payload(source_hint="rule-i-just-made-up"))
    payload = validate(raw, RULES, FEATURES)
    ok = payload.source_hint is None and any("source_hint" in note for note in payload.notes)
    check("6 schema: unknown source_hint -> null", ok, str(payload.notes))


# ---------------------------------------------------------------- 7. options out of range

def test_options_out_of_range() -> None:
    raw = json.loads(
        good_payload(
            status="CLARIFY",
            components=[],
            source_hint=None,
            clarify={"question": "Which one?", "options": ["bottle", "spaceship"]},
        )
    )
    payload = validate(raw, RULES, FEATURES)
    ok = payload.clarify is not None and payload.clarify["options"] == list(FEATURES.ids)
    check("7 schema: invented option -> fixed option list", ok, str(payload.clarify))


# ------------------------------------------------- 8. two questions, then decide

CLARIFY_PAYLOAD = good_payload(
    status="CLARIFY",
    confidence=0.15,
    product={"name": "the thing from the canteen", "brand": ""},
    components=[],
    source_hint=None,
    clarify={"question": "Which packaging do you see?", "options": list(FEATURES.ids[:6])},
)


def test_third_round_commits_to_a_conclusion() -> None:
    """Two questions is the budget - but the budget must never end in a shrug.

    "I still cannot tell, check it yourself" is a refusal wearing the mask of a
    conclusion. The third round spends one more model call that is forbidden to
    ask again, and reports its guess as a guess.
    """
    committed = good_payload(
        confidence=0.45,
        product={"name": "canteen drink", "brand": ""},
        components=[{"part": "carton", "material": "tetra pak", "confidence": 0.45}],
        source_hint="beverage-carton",
    )
    client = FakeClient("openai", [CLARIFY_PAYLOAD, CLARIFY_PAYLOAD, CLARIFY_PAYLOAD, committed])
    pipeline = Pipeline(RULES, FEATURES, router=Router([client], timeout=5))
    first = pipeline.identify("the thing from the canteen")
    second = pipeline.identify("the thing from the canteen", session_id=first["session_id"])
    third = pipeline.identify("the thing from the canteen", session_id=first["session_id"])
    ok = (
        first["status"] == "CLARIFY"
        and second["status"] == "CLARIFY"
        and third["status"] == "OK"
        and third["mode"] == "best_guess"
        and client.calls == 4  # 3 rounds + 1 commit
        and third["confidence"] <= 0.6  # a guess is never dressed up as certain
    )
    check("8 session: third round commits instead of refusing", ok, f"{first['status']}/{second['status']}/{third['status']} mode={third.get('mode')}")


def test_commit_survives_a_model_that_asks_again() -> None:
    """The model ignored 'do not ask again'. We do not guess the first option -
    that would name the wrong material. We give a labelled best-effort move.
    """
    client = FakeClient("openai", [CLARIFY_PAYLOAD])
    pipeline = Pipeline(RULES, FEATURES, router=Router([client], timeout=5))
    first = pipeline.identify("the thing from the canteen")
    second = pipeline.identify("the thing from the canteen", session_id=first["session_id"])
    third = pipeline.identify("the thing from the canteen", session_id=first["session_id"])
    message = third.get("message") or ""
    ok = (
        third["status"] == "REFUSE"
        and "best_effort" in (third.get("reason") or "")
        and "re Kiosk" in message
        and "check it yourself" not in message
    )
    check(
        "8b session: model asks again on the commit round -> best-effort, not a blind guess",
        ok,
        f"{third['status']} reason={third.get('reason')} msg={message[:60]}",
    )


def test_commit_survives_a_dead_model() -> None:
    """The model dies on the commit call. The answer is still not
    service_unavailable - the user already gave us two rounds of context, and a
    best-effort move beats a blank error.
    """
    # The two questions succeed; only the final commit call dies.
    client = FakeClient("openai", [CLARIFY_PAYLOAD, CLARIFY_PAYLOAD, CLARIFY_PAYLOAD, TimeoutError("down")])
    pipeline = Pipeline(RULES, FEATURES, router=Router([client], timeout=0.2))
    first = pipeline.identify("the thing from the canteen")
    second = pipeline.identify("the thing from the canteen", session_id=first["session_id"])
    third = pipeline.identify("the thing from the canteen", session_id=first["session_id"])
    message = third.get("message") or ""
    ok = (
        third["status"] == "REFUSE"
        and third.get("mode") != "service_unavailable"
        and "best_effort" in (third.get("reason") or "")
        and "re Kiosk" in message
    )
    check(
        "8c session: model dies while committing -> best-effort, never service_unavailable",
        ok,
        f"{third['status']} mode={third.get('mode')} msg={message[:60]}",
    )


def test_commit_with_no_usable_signal_still_gives_a_move() -> None:
    """The last resort: nothing resolves, so hand over one concrete action.

    Reachable when the pending question came from a product search: its options
    are product names, not packaging feature ids, so none of them maps to a rule.
    """
    pipeline = Pipeline(RULES, FEATURES, router=Router([FakeClient("openai", [CLARIFY_PAYLOAD])], timeout=5))
    session = pipeline.sessions.get_or_create()
    session.add_turn("user", "lemon")
    session.remember(last_text="lemon")
    result = pipeline._commit(session, {"options": ["Lemon Black Tea", "Lemon Mayonnaise"]}, None)
    message = result.get("message") or ""
    ok = result["status"] == "REFUSE" and "re Kiosk" in message and "check it yourself" not in message
    check("8d last resort: no resolvable option -> one concrete next move, never a shrug", ok, message[:70])


def test_single_option_clarify_expanded() -> None:
    raw = json.loads(
        good_payload(
            status="CLARIFY",
            components=[],
            source_hint=None,
            clarify={"question": "Plastic or paper?", "options": ["cup"]},
        )
    )
    payload = validate(raw, RULES, FEATURES)
    ok = payload.clarify is not None and len(payload.clarify["options"]) >= 2
    check("9 schema: one-option clarify -> full option list", ok, str(payload.clarify))


# ------------------------------------------------- 10-12. brand disambiguation

BRAND_PAYLOAD = json.dumps(
    {
        "status": "BRAND",
        "confidence": 0.8,
        "product": {"name": "coca cola", "brand": "Coca-Cola"},
        "components": [],
        "clarify": None,
        "products": None,
        "explanations": {},
        "search_used": False,
        "source_hint": None,
    }
)


def test_brand_payload_keeps_products() -> None:
    raw = json.loads(
        good_payload(
            status="BRAND",
            components=[],
            source_hint=None,
            products=[
                {"name": "Coke Zero 330ml can", "packaging": "aluminium can"},
                {"no_name_here": True},
                "junk",
                {"name": "Coke 500ml bottle", "packaging": "pet bottle"},
            ],
        )
    )
    payload = validate(raw, RULES, FEATURES)
    ok = (
        payload.status == "BRAND"
        and [p["name"] for p in payload.products] == ["Coke Zero 330ml can", "Coke 500ml bottle"]
        and any("products" in note for note in payload.notes)
    )
    check("10 schema: BRAND keeps cleaned product candidates", ok, str(payload.products))


def test_brand_disambiguation_flow() -> None:
    main = FakeClient("openai", [BRAND_PAYLOAD])
    search = FakeClient(
        "openai",
        [
            json.dumps(
                {
                    "products": [
                        {"name": "Coca-Cola Classic 330ml can", "packaging": "aluminium can"},
                        {"name": "Coca-Cola Zero 500ml bottle", "packaging": "pet bottle"},
                    ]
                }
            )
        ],
    )
    pipeline = Pipeline(
        RULES,
        FEATURES,
        router=Router([main], timeout=5),
        search_router=Router([search], timeout=5),
    )
    result = pipeline.identify("coca cola")
    clarify = result.get("clarify") or {}
    ok = (
        result["status"] == "CLARIFY"
        and clarify.get("kind") == "disambiguate"
        and clarify.get("source") == "ai-search"
        and clarify.get("options")
        == ["Coca-Cola Classic 330ml can", "Coca-Cola Zero 500ml bottle"]
        and clarify.get("hints", {}).get("Coca-Cola Zero 500ml bottle") == "pet bottle"
        and result.get("search_used") is True
        and main.calls == 1
        and search.calls == 1
    )
    check("11 pipeline: brand-only -> web-search disambiguation", ok, str(clarify))


def test_brand_without_search_degrades() -> None:
    main = FakeClient("openai", [BRAND_PAYLOAD])
    search = FakeClient("openai", ["this is not json at all"])
    pipeline = Pipeline(
        RULES,
        FEATURES,
        router=Router([main], timeout=5),
        search_router=Router([search], timeout=5),
    )
    result = pipeline.identify("coca cola")
    # A failed search must NOT synthesise a "(no packaging)" option - that
    # would pretend coca cola might come loose, the same error the
    # not_packaging rule exists to stop. It must also NOT drop back to
    # "which packaging do you see?" - that question assumes a packaging the
    # user may not have. The honest move is a best-effort card that names
    # one concrete next step (re Kiosk / tell me the container word).
    ok = (
        result["status"] == "REFUSE"
        and "best_effort" in (result.get("reason") or "")
        and "re Kiosk" in (result.get("message") or "")
        and search.calls == 1
    )
    check("12 pipeline: failed search -> best-effort card, never a guess", ok, str(result.get("message", ""))[:100])


# ------------------------------------------------- 13. refusal reason passthrough

def test_refusal_reason_passthrough() -> None:
    keep = validate(json.loads(good_payload(status="REFUSE", components=[], reason="not_packaging")), RULES, FEATURES)
    drop = validate(json.loads(good_payload(status="REFUSE", components=[], reason="i feel like it")), RULES, FEATURES)
    plain = validate(json.loads(good_payload(status="REFUSE", components=[])), RULES, FEATURES)
    ok = (
        keep.status == "REFUSE"
        and keep.reason == "not_packaging"
        and drop.reason == ""
        and any("reason" in note for note in drop.notes)
        and plain.reason == ""
    )
    check("13 schema: not_packaging reason kept, invented reasons dropped", ok, f"keep={keep.reason} drop={drop.reason}")


def test_refuse_not_packaging_copy() -> None:
    main = FakeClient(
        "openai",
        [json.dumps({"status": "REFUSE", "reason": "not_packaging", "confidence": 0.7, "components": []})],
    )
    pipeline = Pipeline(RULES, FEATURES, router=Router([main], timeout=5))
    result = pipeline.identify("banana")
    ok = result["status"] == "REFUSE" and result["message"] == refusal.NOT_PACKAGING
    check("14 pipeline: model not_packaging -> dedicated copy", ok, result.get("message", ""))


def test_refuse_message_layers() -> None:
    # Model-written message wins; missing message falls back to refusal.py copy.
    scripted = FakeClient(
        "openai",
        [
            json.dumps(
                {
                    "status": "REFUSE",
                    "reason": "not_packaging",
                    "message": " This is a banana -  nothing to recycle!  ",
                    "confidence": 0.7,
                    "components": [],
                }
            ),
            json.dumps({"status": "REFUSE", "reason": "not_packaging", "confidence": 0.7, "components": []}),
            json.dumps({"status": "OK", "message": "sneaky", "components": [{"part": "cup", "material": "paper", "confidence": 0.9}]}),
        ],
    )
    pipeline = Pipeline(RULES, FEATURES, router=Router([scripted], timeout=5))
    first = pipeline.identify("banana")
    second = pipeline.identify("banana", session_id=first["session_id"])
    third = pipeline.identify("banana", session_id=first["session_id"])
    ok = (
        first["message"] == "This is a banana - nothing to recycle!"
        and second["message"] == refusal.NOT_PACKAGING
        and third["message"] != "sneaky"  # OK must not carry a message
        and scripted.calls == 3
    )
    check("15 pipeline: model message wins, fixed copy is the fallback", ok, f"{first['message']!r} / {second['message']!r}")


def test_unpacked_candidate_flow() -> None:
    main = FakeClient("openai", [BRAND_PAYLOAD])
    search = FakeClient(
        "openai",
        [
            json.dumps(
                {
                    "products": [
                        {"name": "Whole lemon (no packaging)", "packaging": "none"},
                        {"name": "Vita Lemon Tea 250ml", "packaging": "tetra pak carton"},
                        {"name": "Vita Lemon Tea 500ml", "packaging": "pet bottle"},
                    ]
                }
            )
        ],
    )
    pipeline = Pipeline(
        RULES,
        FEATURES,
        router=Router([main], timeout=5),
        search_router=Router([search], timeout=5),
    )
    first = pipeline.identify("lemon")
    clarify = first.get("clarify") or {}
    unpacked_first = clarify.get("options", [None])[0] == "Whole lemon (no packaging)"
    second = pipeline.identify("no packaging: Whole lemon", session_id=first["session_id"])
    ok = (
        first["status"] == "CLARIFY"
        and clarify.get("kind") == "disambiguate"
        and unpacked_first
        and second["status"] == "REFUSE"
        and second["reason"] == "unpacked choice: Whole lemon"
        and "Whole lemon" in second["message"]
        and main.calls == 1  # the no-packaging gate is deterministic, no model call
    )
    check("16 pipeline: unpackaged candidate -> deterministic not_packaging", ok, str(second.get("message", "")))


def test_bulk_material_never_refused_as_unpackaged() -> None:
    # "tea" is sold in tins, bottles, cartons and tea bags. Even when the model
    # calls it "an item without packaging", the deterministic guard sends it to
    # product search instead of refusing.
    refusal_payload = json.dumps(
        {
            "status": "REFUSE",
            "reason": "not_packaging",
            "message": "A Loose tea leaves comes without any packaging.",
            "confidence": 0.8,
            "components": [],
        }
    )
    main = FakeClient("openai", [refusal_payload])
    search = FakeClient(
        "openai",
        [json.dumps({"products": [{"name": "Tao Ti Green Tea 1.5L", "packaging": "pet bottle"}]})],
    )
    pipeline = Pipeline(
        RULES,
        FEATURES,
        router=Router([main], timeout=5),
        search_router=Router([search], timeout=5),
    )
    result = pipeline.identify("tea")
    clarify = result.get("clarify") or {}
    ok = (
        result["status"] == "CLARIFY"
        and clarify.get("kind") == "disambiguate"
        and clarify.get("options") == ["Tao Ti Green Tea 1.5L"]
        and search.calls == 1
    )
    check("17 pipeline: bulk word + not_packaging -> product search, not a refusal", ok, str(clarify)[:140])


def test_whole_item_still_refused() -> None:
    # The guard must not swallow genuine unpackaged items.
    main = FakeClient(
        "openai",
        [
            json.dumps(
                {
                    "status": "REFUSE",
                    "reason": "not_packaging",
                    "message": "A whole banana comes without packaging - there is nothing to recycle here.",
                    "confidence": 0.9,
                    "components": [],
                }
            )
        ],
    )
    pipeline = Pipeline(RULES, FEATURES, router=Router([main], timeout=5))
    result = pipeline.identify("banana")
    ok = result["status"] == "REFUSE" and result["reason"] == "model: not_packaging" and main.calls == 1
    check("18 pipeline: a whole fruit is still refused as unpackaged", ok, result.get("reason", ""))


def test_bare_noun_never_asks_about_packaging() -> None:
    # The user typed a flavour word and told us nothing about packaging; asking
    # "which packaging do you see?" is unanswerable, so CLARIFY is upgraded to
    # the product search.
    clarify_payload = json.dumps(
        {
            "status": "CLARIFY",
            "confidence": 0.9,
            "components": [],
            "clarify": {"question": "Which packaging do you see?", "options": ["bottle", "cup"]},
        }
    )
    main = FakeClient("openai", [clarify_payload])
    search = FakeClient(
        "openai",
        [json.dumps({"products": [{"name": "Whole lemon (no packaging)", "packaging": "none"}]})],
    )
    pipeline = Pipeline(
        RULES,
        FEATURES,
        router=Router([main], timeout=5),
        search_router=Router([search], timeout=5),
    )
    result = pipeline.identify("lemon")
    clarify = result.get("clarify") or {}
    ok = (
        result["status"] == "CLARIFY"
        and clarify.get("kind") == "disambiguate"
        and clarify.get("options", [None])[0] == "Whole lemon (no packaging)"
        and search.calls == 1
    )
    check("19 pipeline: bare flavour word -> product search, never a packaging quiz", ok, str(clarify)[:140])


def test_container_word_still_asks_about_packaging() -> None:
    # "a bottle" DOES name a packaging part, so the packaging question stands.
    clarify_payload = json.dumps(
        {
            "status": "CLARIFY",
            "confidence": 0.9,
            "components": [],
            "clarify": {"question": "Which packaging do you see?", "options": ["bottle", "glass-bottle"]},
        }
    )
    main = FakeClient("openai", [clarify_payload])
    search = FakeClient("openai", [json.dumps({"products": []})])
    pipeline = Pipeline(
        RULES,
        FEATURES,
        router=Router([main], timeout=5),
        search_router=Router([search], timeout=5),
    )
    result = pipeline.identify("a bottle")
    clarify = result.get("clarify") or {}
    ok = (
        result["status"] == "CLARIFY"
        and "kind" not in clarify
        and clarify.get("options") == ["bottle", "glass-bottle"]
        and search.calls == 0
    )
    check("20 pipeline: a named container keeps the packaging question", ok, str(clarify)[:140])


def test_contradictory_unpacked_candidate_dropped() -> None:
    # Search returned the same chocolate both with and without packaging; the
    # packaged entry is the only true one, so the phantom twin must go.
    main = FakeClient("openai", [BRAND_PAYLOAD])
    search = FakeClient(
        "openai",
        [
            json.dumps(
                {
                    "products": [
                        {"name": "KitKat Cookie Crumble Tablet (no packaging)", "packaging": "none"},
                        {"name": "KitKat Cookie Crumble Tablet", "packaging": "plastic wrapper"},
                        {"name": "KitKat Cookie Crumble Tablet", "packaging": "plastic wrapper"},
                    ]
                }
            )
        ],
    )
    pipeline = Pipeline(
        RULES,
        FEATURES,
        router=Router([main], timeout=5),
        search_router=Router([search], timeout=5),
    )
    clarify = pipeline.identify("chocolate").get("clarify") or {}
    ok = clarify.get("options") == ["KitKat Cookie Crumble Tablet"] and clarify.get("hints", {}).get(
        "KitKat Cookie Crumble Tablet"
    ) == "plastic wrapper"
    check("21 pipeline: 'X (no packaging)' dropped when X is also packaged", ok, str(clarify.get("options")))


def test_misspelled_container_is_not_a_bare_noun() -> None:
    """Adversarial #1: 'bottel' is 1 edit from 'bottle'.

    A bare-noun guard that only checks exact container words would send it to
    product search and surface 'Coca-Cola (no packaging)'. The near-miss
    check keeps it on the packaging path.
    """
    main = FakeClient("openai", [CLARIFY_PAYLOAD])
    pipeline = Pipeline(RULES, FEATURES, router=Router([main], timeout=5))
    result = pipeline.identify("bottel")
    clarify = result.get("clarify") or {}
    # A misspelled container word keeps the packaging question - it is NOT
    # sent to product search as a bare noun.
    ok = result["status"] == "CLARIFY" and "kind" not in clarify and "bottle" in " ".join(
        clarify.get("options", [])
    )
    check("22 adversarial: 'bottel' (typo) keeps the packaging question", ok, str(clarify.get("options"))[:100])


def test_tie_between_two_rules_does_not_pick_first() -> None:
    """Adversarial #2: 'plastic bottle and paper cup' hits two rules at the
    same score. Picking rules.json order (carton-first) would shadow the cup.

    A tie returns None from the fast path so the AI lists every component.
    """
    main = FakeClient("openai", [CLARIFY_PAYLOAD])
    pipeline = Pipeline(RULES, FEATURES, router=Router([main], timeout=5))
    # The fast path must NOT short-circuit on a tie.
    result = pipeline.identify("plastic bottle and paper cup")
    # Either the AI answered (status OK/CLARIFY) or it asked - but it must
    # not have answered from a single rules.json-first match.
    ok = result["status"] in ("OK", "CLARIFY") and result.get("mode") != "exact"
    check("23 adversarial: tied rules -> AI layer, not rules.json-first", ok, f"status={result['status']} mode={result.get('mode')}")


def test_no_packaging_gate_respects_packaging_vocabulary() -> None:
    """Adversarial #3: 'no packaging: battery' must NOT refuse as not_packaging.

    Batteries have their own rule; the deterministic gate must check whether
    the item touches the packaging vocabulary and fall through to the model
    when it does.
    """
    import json as _json
    battery_payload = _json.dumps({
        "status": "OK",
        "confidence": 0.8,
        "components": [{"part": "battery", "material": "alkaline", "confidence": 0.8}],
        "source_hint": "battery",
        "explanations": {"battery": "Batteries have a dedicated collection point."},
        "product": {"name": "battery", "brand": ""},
    })
    main = FakeClient("openai", [battery_payload])
    pipeline = Pipeline(RULES, FEATURES, router=Router([main], timeout=5))
    result = pipeline.identify("no packaging: battery")
    # The gate fell through; the model matched the battery rule.
    ok = result["status"] == "OK" and any(
        c.get("part") == "battery" for c in result.get("components", [])
    )
    check("24 adversarial: 'no packaging: battery' -> model matches battery rule, not refused", ok, f"status={result['status']} reason={result.get('reason')}")


def main() -> int:
    print("AI plugin tests (fake providers, no network)")
    test_schema_accepts_valid()
    test_failover_to_anthropic()
    test_both_timeout()
    test_parse_failure_downgrades()
    test_timeout_no_retry()
    test_invalid_source_hint_nulled()
    test_options_out_of_range()
    test_third_round_commits_to_a_conclusion()
    test_commit_survives_a_model_that_asks_again()
    test_commit_survives_a_dead_model()
    test_commit_with_no_usable_signal_still_gives_a_move()
    test_single_option_clarify_expanded()
    test_brand_payload_keeps_products()
    test_brand_disambiguation_flow()
    test_brand_without_search_degrades()
    test_refusal_reason_passthrough()
    test_refuse_not_packaging_copy()
    test_refuse_message_layers()
    test_unpacked_candidate_flow()
    test_bulk_material_never_refused_as_unpackaged()
    test_whole_item_still_refused()
    test_bare_noun_never_asks_about_packaging()
    test_container_word_still_asks_about_packaging()
    test_contradictory_unpacked_candidate_dropped()
    test_misspelled_container_is_not_a_bare_noun()
    test_tie_between_two_rules_does_not_pick_first()
    test_no_packaging_gate_respects_packaging_vocabulary()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
