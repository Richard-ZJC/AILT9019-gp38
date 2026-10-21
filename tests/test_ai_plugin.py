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


# ---------------------------------------------------------------- 8. clarify budget

def test_third_clarify_refused() -> None:
    pipeline = Pipeline(RULES, FEATURES, router=Router([MockClient(RULES, FEATURES)], timeout=5))
    first = pipeline.identify("the thing from the canteen")
    second = pipeline.identify("the thing from the canteen", session_id=first["session_id"])
    third = pipeline.identify("the thing from the canteen", session_id=first["session_id"])
    ok = (
        first["status"] == "CLARIFY"
        and second["status"] == "CLARIFY"
        and third["status"] == "REFUSE"
        and "two rounds" in third["message"]
    )
    check("8 session: third clarifying question is refused", ok, f"{first['status']}/{second['status']}/{third['status']}")


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
        and clarify.get("options") == ["Coca-Cola Classic 330ml can", "Coca-Cola Zero 500ml bottle"]
        and clarify.get("hints", {}).get("Coca-Cola Zero 500ml bottle") == "pet bottle"
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
    clarify = result.get("clarify") or {}
    ok = (
        result["status"] == "CLARIFY"
        and "kind" not in clarify
        and clarify.get("options") == list(FEATURES.ids)
        and search.calls == 1
    )
    check("12 pipeline: failed search -> ordinary packaging clarify", ok, str(clarify)[:120])


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


def main() -> int:
    print("AI plugin tests (fake providers, no network)")
    test_schema_accepts_valid()
    test_failover_to_anthropic()
    test_both_timeout()
    test_parse_failure_downgrades()
    test_timeout_no_retry()
    test_invalid_source_hint_nulled()
    test_options_out_of_range()
    test_third_clarify_refused()
    test_single_option_clarify_expanded()
    test_brand_payload_keeps_products()
    test_brand_disambiguation_flow()
    test_brand_without_search_degrades()
    test_refusal_reason_passthrough()
    test_refuse_not_packaging_copy()
    test_refuse_message_layers()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
