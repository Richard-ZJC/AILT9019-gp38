#!/usr/bin/env python3
"""Smoke test: boot the real server on a free port and hit every route once.

    python tests/smoke_test.py

Uses the deterministic mock provider, so it passes on a machine with no API key
and no network. All checks must pass - this is the gate before a commit.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFUSAL_COPY = "This packaging is not covered by the current rule set."

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(url: str, payload: dict | None = None, timeout: float = 15) -> tuple[int, dict | str]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, body


def wait_for(base: str, process: subprocess.Popen, seconds: float = 20) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            status, _ = call(f"{base}/health", timeout=1)
            if status == 200:
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.2)
    return False


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"starting server on {base} (mock provider)")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--mock", "--port", str(port)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        if not wait_for(base, process):
            output = ""
            if process.stdout is not None:
                output = process.stdout.read()[-1500:]
            check("server boots", False, output.strip() or "no /health response")
            return 1
        check("server boots and /health answers", True)

        status, body = call(f"{base}/")
        check("GET / serves the page", status == 200 and isinstance(body, str) and "<title>" in body)

        status, body = call(f"{base}/identify", {"text": "Vitasoy lemon tea"})
        ok = (
            status == 200
            and isinstance(body, dict)
            and body.get("status") == "OK"
            and body.get("components")
            and str(body["components"][0].get("source", {}).get("url", "")).startswith("http")
        )
        check("POST /identify (known item) -> 200 OK with a source link", ok, str(body)[:200])

        status, first = call(f"{base}/identify", {"text": "the thing I bought at the canteen"})
        clarify_ok = status == 200 and isinstance(first, dict) and first.get("status") == "CLARIFY"
        check("POST /identify (vague) -> 200 CLARIFY with fixed options", clarify_ok, str(first)[:200])

        answer_ok = False
        if clarify_ok:
            session_id = first.get("session_id")
            choice = (first.get("clarify") or {}).get("options", [None])[0]
            status, second = call(f"{base}/answer", {"session_id": session_id, "choice": choice})
            answer_ok = status == 200 and isinstance(second, dict) and second.get("status") in ("OK", "REFUSE")
            check("POST /answer -> 200 and a final status", answer_ok, str(second)[:200])
        else:
            check("POST /answer -> 200 and a final status", False, "skipped, no clarify turn")

        status, body = call(f"{base}/identify", {"text": "paint thinner"})
        ok = status == 200 and isinstance(body, dict) and body.get("status") == "REFUSE" and body.get("message") == REFUSAL_COPY
        check("POST /identify (out of scope) -> REFUSE with the fixed copy", ok, str(body)[:200])

        status, body = call(f"{base}/identify/stream", {"text": "Vitasoy lemon tea"})
        ok = status == 501 and isinstance(body, dict) and body.get("status") == "UNAVAILABLE"
        check("POST /identify/stream -> 501 UNAVAILABLE (reserved)", ok, f"{status} {body}")

        status, body = call(f"{base}/identify", {"text": ""})
        check("POST /identify (empty text) -> 400", status == 400, f"{status} {body}")

        status, body = call(f"{base}/answer", {"session_id": "does-not-exist", "choice": "bottle"})
        check("POST /answer (unknown session) -> 404", status == 404, f"{status} {body}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} smoke checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
