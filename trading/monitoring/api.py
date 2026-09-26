"""Minimal control HTTP API (stdlib) — health, status, halt, resume, kill.

No required external SaaS. Bind locally on Windows (default 127.0.0.1:8787).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from trading.config import Settings
from trading.journal.db import JournalDB
from trading.monitoring.alerts import AlertSink
from trading.monitoring.control import ControlStore
from trading.monitoring.heartbeat import HeartbeatStore
from trading.monitoring.status import build_status_report


def _json_bytes(payload: dict[str, Any], *, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload, indent=2).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


def make_handler(
    settings: Settings,
    *,
    db_path: str | None = None,
) -> type[BaseHTTPRequestHandler]:
    path = db_path or settings.database.path

    class ControlHandler(BaseHTTPRequestHandler):
        server_version = "AgenticTradingControl/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter default
            return

        def _db(self) -> JournalDB:
            db = JournalDB(path)
            db.migrate()
            return db

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            db = self._db()
            try:
                if route in ("/", "/health"):
                    HeartbeatStore(
                        db,
                        stale_after_seconds=settings.monitoring.heartbeat_stale_seconds,
                    ).beat("control-api", {"route": route})
                    report = build_status_report(settings, db)
                    status_code = 200 if report.healthy and not report.control.kill_switch else 503
                    # /health stays 200 if process is up; include flags.
                    if route == "/health":
                        status_code = 200
                        payload = {
                            "ok": True,
                            "healthy": report.healthy,
                            "control": report.control.to_dict(),
                            "message": report.message,
                        }
                    else:
                        payload = report.to_dict()
                    code, body, ctype = _json_bytes(payload, status=status_code)
                    self._send(code, body, ctype)
                    return

                if route == "/status":
                    HeartbeatStore(
                        db,
                        stale_after_seconds=settings.monitoring.heartbeat_stale_seconds,
                    ).beat("control-api", {"route": route})
                    report = build_status_report(settings, db)
                    code, body, ctype = _json_bytes(report.to_dict())
                    self._send(code, body, ctype)
                    return

                code, body, ctype = _json_bytes(
                    {"error": "not_found", "path": route}, status=404
                )
                self._send(code, body, ctype)
            finally:
                db.close()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            body_json = self._read_json()
            db = self._db()
            alerts = AlertSink(
                webhook_url=settings.monitoring.webhook_url,
                enabled=settings.monitoring.alerts_enabled,
            )
            try:
                store = ControlStore(db)
                if route == "/halt":
                    reason = str(body_json.get("reason") or "HALTED")
                    state = store.halt(reason, source="api")
                    alerts.emit("warning", "HALTED", reason, state.to_dict())
                    code, body, ctype = _json_bytes({"ok": True, "control": state.to_dict()})
                    self._send(code, body, ctype)
                    return
                if route == "/resume":
                    clear_kill = bool(body_json.get("clear_kill", False))
                    state = store.resume(clear_kill=clear_kill, source="api")
                    alerts.emit("info", "RESUMED", "trading resumed", state.to_dict())
                    code, body, ctype = _json_bytes({"ok": True, "control": state.to_dict()})
                    self._send(code, body, ctype)
                    return
                if route == "/kill":
                    reason = str(body_json.get("reason") or "KILL_SWITCH")
                    state = store.kill(reason, source="api")
                    alerts.emit("critical", "KILL_SWITCH", reason, state.to_dict())
                    code, body, ctype = _json_bytes({"ok": True, "control": state.to_dict()})
                    self._send(code, body, ctype)
                    return
                if route == "/clear-kill":
                    state = store.clear_kill(source="api")
                    # Still halted unless resume was also requested.
                    code, body, ctype = _json_bytes({"ok": True, "control": state.to_dict()})
                    self._send(code, body, ctype)
                    return

                code, body, ctype = _json_bytes(
                    {"error": "not_found", "path": route}, status=404
                )
                self._send(code, body, ctype)
            finally:
                db.close()

    return ControlHandler


def serve_control_api(
    settings: Settings,
    *,
    host: str | None = None,
    port: int | None = None,
    blocking: bool = True,
) -> ThreadingHTTPServer:
    """Start the control API. Returns the server (caller may shutdown)."""
    bind_host = host or settings.monitoring.api_host
    bind_port = port if port is not None else settings.monitoring.api_port
    handler = make_handler(settings)
    server = ThreadingHTTPServer((bind_host, bind_port), handler)
    if blocking:
        server.serve_forever()
    else:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    return server


# Convenience for tests
ControlServerFactory = Callable[..., ThreadingHTTPServer]
