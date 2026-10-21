"""Dual-channel routing: try each provider once, in order, with a hard deadline.

Timeout means failover, never retry (worst case 20s instead of 40s). Parse
failures are retried once by identify.py, which is a fast failure - that is a
different concern from network timeouts.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

# Unavailable-copy lives in refusal.SERVICE_DOWN - never duplicate it here.


@dataclass
class Attempt:
    provider: str
    outcome: str  # "ok" | "timeout" | "error"
    elapsed: float
    detail: str = ""


@dataclass
class RouterResult:
    status: str  # "OK" | "SERVICE_UNAVAILABLE"
    text: str = ""
    raw_model: str = ""
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    def summary(self) -> str:
        return " -> ".join(f"{attempt.provider}:{attempt.outcome}" for attempt in self.attempts) or "no providers"


class Router:
    def __init__(self, clients: Sequence[Any], timeout: float = 10.0) -> None:
        self.clients = list(clients)
        self.timeout = timeout

    def _call(self, client: Any, messages: Sequence[dict[str, Any]], image: str | None):
        """Run one provider call under a hard deadline."""
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(client.complete, messages, image)
        try:
            return future.result(timeout=self.timeout), None
        except (FutureTimeout, TimeoutError) as exc:  # 3.10 keeps them distinct
            future.cancel()
            return None, ("timeout", str(exc) or f"no answer within {self.timeout:g}s")
        except BaseException as exc:  # noqa: BLE001 - provider errors are data here
            kind = "timeout" if getattr(exc, "kind", "") == "timeout" else "error"
            return None, (kind, str(exc))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def complete(self, messages: Sequence[dict[str, Any]], image: str | None = None) -> RouterResult:
        attempts: list[Attempt] = []
        for client in self.clients:
            started = time.monotonic()
            text, failure = self._call(client, messages, image)
            elapsed = time.monotonic() - started
            name = getattr(client, "name", client.__class__.__name__)
            if failure is None:
                attempts.append(Attempt(provider=name, outcome="ok", elapsed=elapsed))
                return RouterResult(status="OK", text=str(text or ""), raw_model=name, attempts=attempts)
            outcome, detail = failure
            attempts.append(Attempt(provider=name, outcome=outcome, elapsed=elapsed, detail=detail[:200]))

        return RouterResult(status="SERVICE_UNAVAILABLE", raw_model="", attempts=attempts)

    def stream(self, messages: Sequence[dict[str, Any]], image: str | None = None) -> Iterator[str]:
        raise NotImplementedError("streaming is a should-have (T13); use the non-streaming endpoint")

    def provider_names(self) -> list[str]:
        return [getattr(client, "name", client.__class__.__name__) for client in self.clients]
