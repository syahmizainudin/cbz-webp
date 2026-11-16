"""Local web console for the convert and download queues (stdlib http.server, bound to 127.0.0.1)."""

from __future__ import annotations

import json
import os
import string
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import core
from .download import Auth, DownloadQueue
from .jobqueue import JobQueue

UI_HTML = (Path(__file__).parent / "ui.html").read_bytes()


def list_dirs(raw: str | None) -> dict:
    """Folders inside ``raw`` (default: home) for the page's folder picker. Read-only."""
    p = core.normalize_path(raw) if raw and raw.strip() else Path.home()
    if not p.exists():
        raise ValueError(f"not found: {p}")
    if not p.is_dir():
        raise ValueError(f"not a folder: {p}")
    dirs, archives = [], 0
    with os.scandir(p) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    dirs.append(e.name)
                elif Path(e.name).suffix.lower() in core.ARCHIVE_EXTS:
                    archives += 1
            except OSError:
                continue
    shortcuts = [{"label": "Home", "path": str(Path.home())}]
    for letter in string.ascii_lowercase:
        drive = Path("/mnt") / letter
        if drive.is_dir():
            shortcuts.append({"label": f"{letter.upper()}:", "path": str(drive)})
    return {
        "path": str(p),
        "parent": str(p.parent) if p.parent != p else None,
        "dirs": sorted(dirs, key=str.lower),
        "archives": archives,
        "shortcuts": shortcuts,
    }


def make_handler(queue: JobQueue, defaults: dict, downloads: DownloadQueue | None = None):
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
            return {
                "jobs": queue.snapshot(),
                "paused": queue.paused,
                "workers": queue.workers,
                "image_threads": queue.image_threads,
                "defaults": defaults,
                "downloads": downloads.snapshot() if downloads else [],
                "downloads_paused": downloads.paused if downloads else False,
                "download_workers": downloads.workers if downloads else 0,
            }

        # ---------------------------------------------------- routes
        def do_GET(self):
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, b"bad host", "text/plain")
            url = urlsplit(self.path)
            path = url.path
            if path == "/":
                return self._send(200, UI_HTML, "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(self._state())
            if path == "/api/dirs":
                try:
                    raw = parse_qs(url.query).get("path", [""])[0]
                    return self._json(list_dirs(raw))
                except (OSError, ValueError) as e:
                    return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
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
                        paths,
                        int(body.get("quality", defaults["quality"])),
                        delete_original=bool(body.get("delete_original", True)),
                        overwrite=bool(body.get("overwrite", False)),
                        recursive=bool(body.get("recursive", True)),
                    )
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
                if path.startswith("/api/download/") and downloads:
                    return self._download(path.removeprefix("/api/download/"), body)
            except (OSError, ValueError, KeyError) as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def _download(self, action: str, body: dict):
            if action == "add":
                urls = body.get("urls", [])
                if not isinstance(urls, list):
                    raise ValueError("urls must be a list")
                a = body.get("auth") or {}
                auth = Auth(str(a.get("mode", "none")), str(a.get("key", "")), str(a.get("name", "")))
                added, dupes = downloads.add(
                    [str(u) for u in urls],
                    str(body.get("dest", "")).strip() or "",
                    auth,
                    convert=bool(body.get("convert", True)),
                    quality=int(body.get("quality", defaults["quality"])),
                    delete_original=bool(body.get("delete_original", True)),
                )
                return self._json({"added": len(added), "duplicates": dupes})
            if action == "cancel":
                return self._json({"ok": downloads.cancel(int(body["id"]))})
            if action == "cancel-all":
                return self._json({"cancelled": downloads.cancel_all()})
            if action == "retry":
                return self._json({"ok": downloads.retry(int(body["id"]))})
            if action == "clear":
                return self._json({"cleared": downloads.clear_finished()})
            if action == "pause":
                downloads.set_paused(bool(body.get("paused", True)))
                return self._json({"paused": downloads.paused})
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(
    queue: JobQueue, port: int, defaults: dict, downloads: DownloadQueue | None = None
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(queue, defaults, downloads))
    server.daemon_threads = True
    return server
