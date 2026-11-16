"""Local web console for the queue (stdlib http.server, bound to 127.0.0.1)."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .jobqueue import JobQueue

UI_HTML = (Path(__file__).parent / "ui.html").read_bytes()


def make_handler(queue: JobQueue, defaults: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cbz-webp"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        # ---------------------------------------------------- helpers
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            return urlsplit(origin).netloc == self.headers.get("Host")

        def _state(self) -> dict:
            return {"jobs": queue.snapshot(), "paused": queue.paused,
                    "workers": queue.workers, "image_threads": queue.image_threads,
                    "defaults": defaults}

        # ---------------------------------------------------- routes
        def do_GET(self):
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, b"bad host", "text/plain")
            path = urlsplit(self.path).path
            if path == "/":
                return self._send(200, UI_HTML, "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(self._state())
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

        def do_POST(self):
            # This page can delete files: refuse anything a cross-site form could send.
            if not (self._host_ok() and self._origin_ok()):
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._json({"error": "JSON only"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._json({"error": "bad JSON"}, HTTPStatus.BAD_REQUEST)

            path = urlsplit(self.path).path
            try:
                if path == "/api/add":
                    paths = [p for p in body.get("paths", []) if str(p).strip()]
                    if not paths:
                        return self._json({"error": "give at least one folder or file"}, 400)
                    added, dupes = queue.add(
                        paths, int(body.get("quality", defaults["quality"])),
                        delete_original=bool(body.get("delete_original", True)),
                        overwrite=bool(body.get("overwrite", False)),
                        recursive=bool(body.get("recursive", True)))
                    return self._json({"added": len(added), "duplicates": dupes})
                if path == "/api/cancel":
                    return self._json({"ok": queue.cancel(int(body["id"]))})
                if path == "/api/cancel-all":
                    return self._json({"cancelled": queue.cancel_all()})
                if path == "/api/retry":
                    return self._json({"ok": queue.retry(int(body["id"]))})
                if path == "/api/clear":
                    return self._json({"cleared": queue.clear_finished()})
                if path == "/api/pause":
                    queue.set_paused(bool(body.get("paused", True)))
                    return self._json({"paused": queue.paused})
            except (OSError, ValueError, KeyError) as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(queue: JobQueue, port: int, defaults: dict) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(queue, defaults))
    server.daemon_threads = True
    return server
