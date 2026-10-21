"""config.json loading: template in the repo, real keys gitignored, env wins."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"

DEFAULTS: dict[str, Any] = {
    "providers": ["openai", "anthropic"],
    "timeout_seconds": 10,
    "max_clarify_rounds": 2,
    "enable_search": True,
    "save_memory": False,
    "debug": False,
    "openai": {"api_key": "", "model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1"},
    "anthropic": {"api_key": "", "model": "claude-3-5-haiku-latest", "base_url": "https://api.anthropic.com"},
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read config.json if present (else the example), then apply env overrides."""
    file_path = Path(path) if path else CONFIG_PATH
    if not file_path.exists():
        file_path = EXAMPLE_PATH

    config = dict(DEFAULTS)
    if file_path.exists():
        try:
            config = _merge(DEFAULTS, json.loads(file_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            config = dict(DEFAULTS)

    env_map = {
        "OPENAI_API_KEY": ("openai", "api_key"),
        "OPENAI_BASE_URL": ("openai", "base_url"),
        "OPENAI_MODEL": ("openai", "model"),
        "ANTHROPIC_API_KEY": ("anthropic", "api_key"),
        "ANTHROPIC_BASE_URL": ("anthropic", "base_url"),
        "ANTHROPIC_MODEL": ("anthropic", "model"),
    }
    for env_name, (section, key) in env_map.items():
        value = os.environ.get(env_name)
        if value:
            config.setdefault(section, {})[key] = value

    config["_source"] = str(file_path)
    return config
