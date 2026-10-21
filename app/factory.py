"""Wire the objects together (the only place that knows about every module)."""

from __future__ import annotations

from typing import Any, Sequence

from .ai_plugin.anthropic_client import AnthropicClient
from .ai_plugin.mock_client import MockClient
from .ai_plugin.openai_client import OpenAIClient
from .ai_plugin.router import Router
from .memory import Memory
from .pipeline import Pipeline
from .rules_loader import FeatureSet, Rule
from .session import SessionStore


def build_clients(
    rules: Sequence[Rule],
    features: FeatureSet,
    config: dict[str, Any],
    force_mock: bool = False,
    enable_search: bool = False,
) -> tuple[list[Any], list[str]]:
    timeout = float(config.get("timeout_seconds", 10))
    notes: list[str] = []

    if force_mock or "mock" in config.get("providers", []):
        notes.append("mock provider enabled - deterministic offline answers, no real model is called")
        return [MockClient(rules, features)], notes

    clients: list[Any] = []
    for name in config.get("providers", []):
        section = config.get(name) or {}
        api_key = str(section.get("api_key") or "")
        if name == "openai":
            if not api_key:
                notes.append("openai: skipped, no api key")
                continue
            extra_body = dict(section.get("extra_body") or {})
            if enable_search:
                # Qwen/DashScope-compatible web search. Only used by the
                # brand-disambiguation router, never by the main identify call.
                # forced_search: never let the model skip the search on a
                # disambiguation call (official search_options switch).
                extra_body["enable_search"] = True
                extra_body.setdefault("search_options", {})["forced_search"] = True
            clients.append(
                OpenAIClient(
                    api_key=api_key,
                    model=str(section.get("model") or "gpt-4o-mini"),
                    base_url=str(section.get("base_url") or "https://api.openai.com/v1"),
                    timeout=timeout,
                    extra_body=extra_body,
                )
            )
        elif name == "anthropic":
            if not api_key:
                notes.append("anthropic: skipped, no api key")
                continue
            clients.append(
                AnthropicClient(
                    api_key=api_key,
                    model=str(section.get("model") or "claude-3-5-haiku-latest"),
                    base_url=str(section.get("base_url") or "https://api.anthropic.com"),
                    timeout=timeout,
                    extra_body=section.get("extra_body") or {},
                )
            )
        else:
            notes.append(f"{name}: unknown provider, ignored")

    if not clients:
        notes.append("no provider with a usable key - only the zero-cost keyword layer will answer")
    return clients, notes


def build_pipeline(
    rules: Sequence[Rule],
    features: FeatureSet,
    config: dict[str, Any],
    force_mock: bool = False,
) -> tuple[Pipeline, list[str]]:
    clients, notes = build_clients(rules, features, config, force_mock=force_mock)
    timeout = float(config.get("timeout_seconds", 10))
    router = Router(clients, timeout=timeout) if clients else None

    search_router: Router | None = None
    if router is not None and bool(config.get("enable_search", True)) and not force_mock:
        search_clients, _ = build_clients(rules, features, config, enable_search=True)
        if search_clients:
            search_router = Router(search_clients, timeout=timeout)

    sessions = SessionStore(max_clarify_rounds=int(config.get("max_clarify_rounds", 2)))
    memory = Memory(enabled=bool(config.get("save_memory", False)))
    pipeline = Pipeline(
        rules=rules,
        features=features,
        router=router,
        sessions=sessions,
        memory=memory,
        debug=bool(config.get("debug", False)),
        search_router=search_router,
    )
    return pipeline, notes
