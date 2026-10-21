#!/usr/bin/env python3
"""Live check against the real providers. Needs a key and network - not part of smoke.

    python tests/live_ai_test.py            # uses config.json / env vars

Exits 0 with "skipped" when no key is configured, so CI machines stay green.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config
from app.factory import build_clients
from app.pipeline import Pipeline
from app.ai_plugin.router import Router
from app.rules_loader import load_features, load_rules

QUERIES = [
    "Vitasoy lemon tea",
    "Nissin cup noodles",
    "a bottle of iced tea from the vending machine",
    "my old laptop battery",
]


def main() -> int:
    rules = load_rules()
    features = load_features()
    config = load_config()
    clients, notes = build_clients(rules, features, config)

    for note in notes:
        print(f"! {note}")
    if not clients:
        print("skipped: no API key configured (set OPENAI_API_KEY or config.json)")
        return 0

    pipeline = Pipeline(rules, features, router=Router(clients, timeout=float(config.get("timeout_seconds", 10))), debug=True)

    for query in QUERIES:
        result = pipeline.identify(query)
        categories = [item.get("category") for item in result.get("components", [])]
        print(f"\n{query!r} -> {result['status']} ({result.get('mode')}, model={result.get('raw_model')})")
        print(f"  categories: {categories}")
        if result["status"] in ("REFUSE", "SERVICE_UNAVAILABLE"):
            print(f"  message: {result.get('message')}")
        if result.get("diagnostics"):
            print(f"  attempts: {result['diagnostics']['attempts']}")
        for item in result.get("components", []):
            if item.get("status") == "MATCHED":
                print(f"  - {item['part']}: {item['category']} [{item['rule_id']}] <- {item['matched_by']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
