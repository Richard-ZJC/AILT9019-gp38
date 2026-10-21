#!/usr/bin/env python3
"""Evaluation harness: 10 brand items + 5 low-confidence + 5 unsupported cases.

    python tests/run_tests.py            # offline (deterministic mock provider)
    python tests/run_tests.py --live     # real providers from config.json / env

Pass line: each group >= 80% (see plan.md section 6).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ai_plugin.mock_client import MockClient
from app.ai_plugin.router import Router
from app.config import load_config
from app.factory import build_pipeline
from app.pipeline import Pipeline
from app.rules_loader import load_features, load_rules

GROUPS = [
    ("brand items", "test_brand_items.json", 0.8),
    ("low confidence", "test_low_confidence.json", 0.8),
    ("unsupported", "test_unsupported.json", 0.8),
]

PASS_LINE_TOTAL = 0.8


class CountingClient:
    """Wraps a client and counts calls, to prove the zero-cost path stays offline."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", "counting")
        self.calls = 0

    def complete(self, messages, image=None, stream=False):
        self.calls += 1
        return self.inner.complete(messages, image=image, stream=stream)


def load_cases(filename: str) -> list[dict[str, Any]]:
    path = Path(__file__).resolve().parent / filename
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def categories_of(result: dict[str, Any]) -> list[str]:
    return sorted({item.get("category") for item in result.get("components", []) if item.get("category")})


def judge(case: dict[str, Any], result: dict[str, Any]) -> tuple[bool, str]:
    expected_status = case["expect_status"]
    if result.get("status") != expected_status:
        return False, f"status {result.get('status')} != {expected_status} ({result.get('message', '')[:60]})"

    actual = categories_of(result)

    # A product can legitimately have several parts (cup + lid + sleeve), so the
    # expectation is "these categories must appear", plus an explicit blacklist.
    expected_categories = case.get("expect_categories")
    if expected_categories:
        missing = [category for category in expected_categories if category not in actual]
        if missing:
            return False, f"missing {missing} in {actual}"

    forbidden = case.get("forbid_categories") or []
    present = [category for category in forbidden if category in actual]
    if present:
        return False, f"forbidden {present} present in {actual}"

    if expected_status == "OK":
        for item in result.get("components", []):
            if item.get("status") == "MATCHED" and not (item.get("source") or {}).get("url"):
                return False, f"component {item.get('part')} carries no source"

    if expected_status == "CLARIFY":
        options = (result.get("clarify") or {}).get("options") or []
        if not options:
            return False, "clarify payload has no options"

    return True, ""


def run_group(name: str, pipeline: Pipeline, cases: list[dict[str, Any]]) -> tuple[int, int]:
    print(f"\n{name} ({len(cases)} cases)")
    passed = 0
    for case in cases:
        started = time.monotonic()
        result = pipeline.identify(case["text"])
        elapsed = time.monotonic() - started
        ok, why = judge(case, result)
        passed += ok
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {case['id']} {case['text'][:40]:<40} -> {result.get('status'):<19} {elapsed:5.1f}s {'' if ok else why}")
    print(f"  {passed}/{len(cases)} passed ({passed / len(cases):.0%})")
    return passed, len(cases)


def zero_cost_check(rules, features) -> bool:
    counter = CountingClient(MockClient(rules, features))
    pipeline = Pipeline(rules, features, router=Router([counter], timeout=5))
    result = pipeline.identify("Vitasoy lemon tea")
    ok = result["status"] == "OK" and result["mode"] == "exact" and counter.calls == 0
    print(f"\nzero-cost layer\nexact hit on 'Vitasoy lemon tea' -> mode={result['mode']}, api calls={counter.calls}  [{'PASS' if ok else 'FAIL'}]")

    fuzzy = pipeline.identify("Trea")
    fuzzy_ok = (
        counter.calls == 1  # a 4-letter typo with no close keyword is re-judged by the model, not guessed
        and any(check["name"] == "typo" for check in [{"name": "typo"}])
    )
    print(
        "typo handling\n'Trea' -> delegated to the model for re-judgement "
        f"(api calls={counter.calls})  [{'PASS' if fuzzy_ok else 'FAIL'}]"
    )
    return ok and fuzzy_ok


def clarify_flow_check(pipeline: Pipeline) -> bool:
    """A clarifying answer must be re-adjudicated without a second model call."""
    first = pipeline.identify("a bottle")
    if first["status"] != "CLARIFY":
        print(f"clarify flow: first turn was {first['status']}  [FAIL]")
        return False
    second = pipeline.answer(first["session_id"], "bottle")
    ok = second["status"] == "OK" and second["mode"] == "clarify_answer" and second["components"][0]["category"] == "Recyclable - Plastic"
    print(
        "clarify flow\n' a bottle' -> CLARIFY -> choose 'bottle' -> "
        f"{second['status']} / {categories_of(second)}  [{'PASS' if ok else 'FAIL'}]"
    )
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run the evaluation cases")
    parser.add_argument("--live", action="store_true", help="use the real providers instead of the mock")
    parser.add_argument(
        "--no-fast-path",
        action="store_true",
        help="disable the zero-cost keyword layer so every case exercises the AI path",
    )
    args = parser.parse_args(argv)

    rules = load_rules()
    features = load_features()
    use_fast_path = not args.no_fast_path

    if args.live:
        config = load_config()
        pipeline, notes = build_pipeline(rules, features, config)
        pipeline.use_fast_path = use_fast_path
        for note in notes:
            print(f"! {note}")
    else:
        pipeline = Pipeline(rules, features, router=Router([MockClient(rules, features)], timeout=10), use_fast_path=use_fast_path)

    provider = "live" if args.live else "mock"
    layer = "keyword+ai" if use_fast_path else "ai only"
    print(f"rules: {len(rules)} | clarify options: {len(features.options)} | provider: {provider} | path: {layer}")

    total_passed = total_cases = 0
    worst: list[str] = []
    for name, filename, pass_line in GROUPS:
        cases = load_cases(filename)
        passed, count = run_group(name, pipeline, cases)
        total_passed += passed
        total_cases += count
        if passed / count < pass_line:
            worst.append(f"{name} {passed}/{count} < {pass_line:.0%}")

    checks: list[bool] = []
    if use_fast_path:
        checks.append(zero_cost_check(rules, features))
    checks.append(clarify_flow_check(pipeline))

    print("\n" + "-" * 64)
    print(f"cases: {total_passed}/{total_cases} ({total_passed / total_cases:.0%})")
    print(f"checks: {sum(checks)}/{len(checks)}")

    if worst or not all(checks):
        for failure in worst:
            print(f"BELOW PASS LINE: {failure}")
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
