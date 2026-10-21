"""Shared HTTP plumbing for the AI providers (standard library only)."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Iterator


class ProviderError(RuntimeError):
    """A provider call failed. `kind` is 'timeout', 'auth', 'http' or 'network'."""

    def __init__(self, message: str, kind: str = "error", status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


def _classify(exc: BaseException, status: int | None = None) -> ProviderError:
    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # pragma: no cover - best effort
            pass
        kind = "auth" if exc.code in (401, 403) else "http"
        return ProviderError(f"HTTP {exc.code}: {detail or exc.reason}", kind=kind, status=exc.code)
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ProviderError(f"timeout: {exc}", kind="timeout", status=status)
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        kind = "timeout" if isinstance(reason, (socket.timeout, TimeoutError)) else "network"
        return ProviderError(f"network: {reason}", kind=kind, status=status)
    return ProviderError(f"{type(exc).__name__}: {exc}", kind="error", status=status)


def _request(url: str, headers: dict[str, str], payload: dict[str, Any]) -> urllib.request.Request:
    data = json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json", "Accept": "application/json"}
    merged.update(headers)
    return urllib.request.Request(url, data=data, headers=merged, method="POST")


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = _request(url, headers, payload)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except BaseException as exc:  # noqa: BLE001 - normalised into ProviderError
        raise _classify(exc) from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"provider returned non-JSON body: {body[:200]}", kind="http") from exc


def post_stream(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    event_prefix: str = "data: ",
) -> Iterator[str]:
    """Yield raw SSE `data:` payloads from a streaming completion."""
    request = _request(url, headers, payload)
    request.add_header("Accept", "text/event-stream")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except BaseException as exc:  # noqa: BLE001
        raise _classify(exc) from exc
    with response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith(event_prefix):
                continue
            chunk = line[len(event_prefix) :].strip()
            if chunk and chunk != "[DONE]":
                yield chunk
