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

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
