"""Local dashboard: tiny stdlib HTTP server reading the SQLite history.

Binds to 127.0.0.1 by default — personal analytics are never exposed to the
network unless DASHBOARD_HOST is changed explicitly.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import socket
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from .config import Config
from .storage import Storage

log = logging.getLogger(__name__)

WEB_ROOT = Path(str(resources.files("yt_report") / "web"))

LOOPBACK_BINDS = {"127.0.0.1", "::1", "localhost"}
LOOPBACK_HOST_NAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}


class DashboardServer(ThreadingHTTPServer):
    """On Windows, SO_REUSEADDR lets a second server bind a port that is already in use, so a second
    dashboard would start silently while the first one keeps answering. Bind exclusively there instead;
    POSIX keeps SO_REUSEADDR (it only allows a quick restart, never two listeners on one port)."""

    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows only
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _host_name(header: str | None) -> str:
    """Host header without the port: 'localhost:8050' -> 'localhost', '[::1]:8050' -> '[::1]'."""
    value = (header or "").strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def make_handler(db_path: Path, mode: str = "production"):
    class Handler(BaseHTTPRequestHandler):
        server_version = "yt-report"

        def log_message(self, fmt, *args):  # route to logging, not stderr
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _host_allowed(self) -> bool:
            # DNS-rebinding protection: on a loopback bind only localhost names are accepted, so a web page
            # on another domain that resolves to 127.0.0.1 cannot read the analytics through the browser.
            # An explicit non-loopback DASHBOARD_HOST (e.g. a Pi on the LAN) is the user's choice and not checked.
            if str(self.server.server_address[0]) not in LOOPBACK_BINDS:
                return True
            return _host_name(self.headers.get("Host")) in LOOPBACK_HOST_NAMES

        def do_GET(self):  # noqa: N802
            if not self._host_allowed():
                return self._send(403, b"Forbidden: unexpected Host header", "text/plain")
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._send(200, (WEB_ROOT / "templates" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/data":
                try:
                    if not db_path.exists():
                        data = {"reports": [], "daily": []}
                    else:
                        with Storage(db_path) as db:
                            data = db.dashboard_data()
                except sqlite3.Error as exc:
                    log.error("Dashboard could not read the database: %s", exc)
                    body = json.dumps({"error": "The local database could not be read. Check logs/yt_report.log."})
                    return self._send(500, body.encode(), "application/json")
                data["mode"] = mode
                return self._send(200, json.dumps(data).encode(), "application/json")
            if path.startswith("/static/"):
                static_root = (WEB_ROOT / "static").resolve()
                target = (static_root / path[len("/static/"):]).resolve()
                if static_root in target.parents and target.is_file():  # blocks path traversal
                    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    return self._send(200, target.read_bytes(), ctype)
            self._send(404, b"Not found", "text/plain")

    return Handler


class DashboardStartError(RuntimeError):
    """The HTTP server could not be started (port in use, not permitted, invalid address)."""


def serve(cfg: Config, host: str, port: int, open_browser: bool = True, db_path: Path | None = None,
          mode: str = "production") -> None:
    db_path = db_path or cfg.database_path
    try:
        httpd = DashboardServer((host, port), make_handler(db_path, mode))
    except (OSError, OverflowError) as exc:
        raise DashboardStartError(
            f"Dashboard could not listen on {host}:{port} ({exc.__class__.__name__}: {exc}). "
            "Is the port already in use? Choose another one with --port or DASHBOARD_PORT."
        ) from exc
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"
    print(f"Dashboard running at {url}  (Ctrl+C to stop)")
    if mode == "demo":
        print("DEMO MODE — synthetic data only; not real channel analytics.")
    elif not db_path.exists():
        print("No database yet — run `python -m yt_report run` first to collect data.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
