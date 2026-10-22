#!/usr/bin/env python3
"""Local HTTP server for Campus Recycling Helper (standard library only).

    python server.py            # real providers from config.json / env
    python server.py --mock     # deterministic offline stand-in (no key needed)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config
from app.factory import build_pipeline
from app.rules_loader import RulesError, load_features, load_rules

WEB_DIR = ROOT / "web"
MAX_BODY_BYTES = 64 * 1024
MAX_QUERY_CHARS = 400

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def make_handler(pipeline):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CampusRecyclingHelper/0.1"
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------------- plumbing

        def log_message(self, fmt: str, *args) -> None:  # keep the console readable
            sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path) -> None:
            if not path.is_file():
                self._send_json(404, {"status": "ERROR", "message": "not found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > MAX_BODY_BYTES:
                return None
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

        # ---------------------------------------------------------- routes

        def do_GET(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            if route in ("/", "/index.html"):
                self._send_file(WEB_DIR / "index.html")
                return
            if route == "/health":
                self._send_json(200, {"ok": True, "service": "campus-recycling-helper"})
                return
            if route == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            candidate = (WEB_DIR / route.lstrip("/")).resolve()
            if candidate.is_file() and str(candidate).startswith(str(WEB_DIR.resolve())):
                self._send_file(candidate)
                return
            self._send_json(404, {"status": "ERROR", "message": "not found"})

        def _access_ok(self) -> bool:
            """If ACCESS_KEY is set, the request must carry it (header or ?key=)."""
            gate = os.environ.get("ACCESS_KEY")
            if not gate:
                return True
            provided = self.headers.get("X-Access-Key", "")
            if not provided:
                provided = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
            return provided == gate

        def do_POST(self) -> None:  # noqa: N802
            if not self._access_ok():
                self._send_json(401, {"status": "ERROR", "message": "access denied: enter the shared key"})
                return
            route = urlparse(self.path).path
            if route == "/identify":
                self._handle_identify()
            elif route == "/answer":
                self._handle_answer()
            elif route == "/identify/stream":
                # should-have T13: reserved, not implemented yet.
                self._send_json(501, {"status": "UNAVAILABLE", "message": "streaming endpoint is not implemented"})
            else:
                self._send_json(404, {"status": "ERROR", "message": "not found"})

        def _handle_identify(self) -> None:
            payload = self._read_json()
            if payload is None:
                self._send_json(400, {"status": "ERROR", "message": "body must be a JSON object"})
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_json(400, {"status": "ERROR", "message": "field 'text' is required"})
                return
            if len(text) > MAX_QUERY_CHARS:
                self._send_json(400, {"status": "ERROR", "message": f"'text' is limited to {MAX_QUERY_CHARS} characters"})
                return
            session_id = payload.get("session_id")
            if session_id is not None and not isinstance(session_id, str):
                self._send_json(400, {"status": "ERROR", "message": "'session_id' must be a string"})
                return
            try:
                result = pipeline.identify(text, session_id=session_id)
            except ValueError as exc:
                self._send_json(400, {"status": "ERROR", "message": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 - never crash the server on one request
                self.log_message("identify failed: %s", exc)
                self._send_json(500, {"status": "ERROR", "message": "internal error"})
                return
            self._send_json(200, result)

        def _handle_answer(self) -> None:
            payload = self._read_json()
            if payload is None:
                self._send_json(400, {"status": "ERROR", "message": "body must be a JSON object"})
                return
            session_id = payload.get("session_id")
            choice = payload.get("choice")
            if not isinstance(session_id, str) or not session_id:
                self._send_json(400, {"status": "ERROR", "message": "field 'session_id' is required"})
                return
            if not isinstance(choice, str) or not choice.strip():
                self._send_json(400, {"status": "ERROR", "message": "field 'choice' is required"})
                return
            try:
                result = pipeline.answer(session_id, choice.strip())
            except LookupError:
                self._send_json(404, {"status": "ERROR", "message": "unknown session_id"})
                return
            except ValueError as exc:
                self._send_json(400, {"status": "ERROR", "message": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self.log_message("answer failed: %s", exc)
                self._send_json(500, {"status": "ERROR", "message": "internal error"})
                return
            self._send_json(200, result)

    return Handler


def serve(host: str, port: int, use_mock: bool) -> int:
    try:
        rules = load_rules()
        features = load_features()
    except RulesError as exc:
        print(f"[fatal] rule set is not usable: {exc}", file=sys.stderr)
        return 2

    config = load_config()
    pipeline, notes = build_pipeline(rules, features, config, force_mock=use_mock)

    httpd = ThreadingHTTPServer((host, port), make_handler(pipeline))
    print(f"Campus Recycling Helper - {len(rules)} rules, {len(features.options)} clarify options")
    print(f"config: {config.get('_source')}")
    for note in notes:
        print(f"  ! {note}")
    print(f"serving on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Campus Recycling Helper server")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)), help="port (default: 8000, or $PORT)")
    parser.add_argument("--mock", action="store_true", help="use the deterministic offline provider instead of a real model")
    args = parser.parse_args(argv)
    return serve(args.host, args.port, args.mock)


if __name__ == "__main__":
    raise SystemExit(main())
