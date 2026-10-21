"""Optional local memory. Not implemented - the interface is the contract."""

from __future__ import annotations

from typing import Any


class Memory:
    """should-have T14: persist sessions to data/memory/. Currently a no-op."""

    enabled = False

    def __init__(self, enabled: bool = False, directory: str | None = None) -> None:
        self.enabled = bool(enabled)
        self.directory = directory

    def record(self, entry: dict[str, Any]) -> None:
        return None

    def last(self, session_id: str) -> None:
        return None
