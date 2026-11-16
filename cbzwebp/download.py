"""Download queue: fetch direct file URLs into a folder, optionally hand archives to the convert queue.

Rules (see AGENTS.md):
  * the API key never appears in job state, logs or messages; in query mode the stored URL has
    no key and it is added only when the request is made;
  * auth headers are sent with ``add_unredirected_header`` so a redirect to another host (a CDN)
    never receives the key;
  * data is streamed to ``<name>.part`` in the destination folder and renamed when complete;
    cancel, failure or a short read removes the ``.part``.
"""

from __future__ import annotations

import os
import re
import threading
from email.message import Message
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import __version__, core
from .jobqueue import ACTIVE, CANCELLED, DONE, FAILED, QUEUED, BaseQueue

AUTH_MODES = ("none", "bearer", "header", "query")
TOKEN_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")  # RFC 9110 header-name token
CHUNK = 256 * 1024
USER_AGENT = f"cbz-webp/{__version__}"


class DownloadCancelled(Exception):
    pass


class DownloadError(Exception):
    pass


@dataclass
class Auth:
    mode: str = "none"  # none | bearer | header | query
    key: str = field(default="", repr=False)
    name: str = ""  # header name (mode=header) or query parameter (mode=query)

    def validate(self) -> Auth:
        if self.mode not in AUTH_MODES:
            raise ValueError(f"auth mode must be one of {', '.join(AUTH_MODES)}")
        if self.mode == "none":
            return Auth()
        if not self.key:
            raise ValueError("API key is empty")
        if any(c in self.key for c in "\r\n\0"):
            raise ValueError("API key contains a line break")
        if self.mode in ("header", "query") and not TOKEN_RE.match(self.name or ""):
            raise ValueError(f"invalid {'header' if self.mode == 'header' else 'parameter'} name")
        return self

    def apply(self, url: str) -> tuple[str, dict[str, str]]:
        """Return the URL to request and the headers that carry the key."""
        if self.mode == "bearer":
            return url, {"Authorization": f"Bearer {self.key}"}
        if self.mode == "header":
            return url, {self.name: self.key}
        if self.mode == "query":
            parts = urlsplit(url)
            query = [*parse_qsl(parts.query, keep_blank_values=True), (self.name, self.key)]
            return urlunsplit(parts._replace(query=urlencode(query))), {}
        return url, {}


def check_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not an http(s) URL: {url}")
    return url


def safe_name(raw: str) -> str:
    """Reduce a server- or URL-supplied name to a bare, Windows-safe file name ('' if nothing is left)."""
    name = unquote(raw).replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip().lstrip(".").strip()
    return name[:200]


def disposition_name(header: str | None) -> str:
    """File name from a Content-Disposition header; RFC 6266 says ``filename*`` wins over ``filename``."""
    if not header:
        return ""
    m = re.search(r"filename\*\s*=\s*([\w!#$%&+^`{}~-]*)'[^']*'([^;\s]+)", header, re.I)
    if m:
        try:
            return unquote(m.group(2), encoding=m.group(1) or "utf-8", errors="strict")
        except (LookupError, UnicodeDecodeError):
            pass
    msg = Message()
    msg["Content-Disposition"] = header
    return msg.get_filename() or ""


@dataclass
class Download:
    id: int
    url: str  # never contains the key
    dest_dir: str
    auth: Auth = field(repr=False)
    convert: bool = True
    quality: int = 80
    delete_original: bool = True
    state: str = QUEUED
    done: int = 0  # bytes received
    total: int = 0  # Content-Length, 0 when the server did not send one
    message: str = ""
    filename: str | None = None
    output: str | None = None
    convert_job_id: int | None = None
    added_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("auth", "cancel")}
        d["auth_mode"] = self.auth.mode  # the key itself is never exposed
        d["name"] = self.filename or safe_name(urlsplit(self.url).path) or self.url
        return d

    def fresh(self) -> Download:
        return Download(
            self.id,
            self.url,
            self.dest_dir,
            self.auth,
            self.convert,
            self.quality,
            self.delete_original,
        )


class DownloadQueue(BaseQueue):
    thread_prefix = "download"

    def __init__(self, workers: int = 2, convert_queue=None, timeout: float = 60, on_change=None):
        super().__init__(workers, on_change)
        self.convert_queue = convert_queue
        self.timeout = timeout
        self._reserved: set[Path] = set()

    # ------------------------------------------------------------ commands
    def add(
        self,
        urls: list[str],
        dest_dir: str | Path,
        auth: Auth | None = None,
        *,
        convert: bool = True,
        quality: int = 80,
        delete_original: bool = True,
    ) -> tuple[list[Download], int]:
        """Queue each URL not already queued/running for the same folder. Returns (added, dupes)."""
        urls = [check_url(u) for u in urls if u.strip()]
        if not urls:
            raise ValueError("give at least one URL")
        if not str(dest_dir).strip():
            raise ValueError("choose a folder to save to")
        dest = core.normalize_path(str(dest_dir))
        if not dest.is_dir():
            raise ValueError(f"save-to folder does not exist: {dest}")
        if convert and not 1 <= int(quality) <= 100:
            raise ValueError("quality must be 1-100")
        auth = (auth or Auth()).validate()
        added, dupes = [], 0
        with self._cond:
            active = {self._dedup_key(j) for j in self._jobs.values() if j.state in ACTIVE}
            for url in urls:
                if (url, str(dest)) in active:
                    dupes += 1
                    continue
                active.add((url, str(dest)))
                added.append(
                    Download(
                        self._new_id(),
                        url,
                        str(dest),
                        auth,
                        convert,
                        int(quality),
                        delete_original,
                    )
                )
            self._enqueue(added)
        for j in added:
            self._emit(j)
        return added, dupes

    def _dedup_key(self, job: Download):
        return (job.url, job.dest_dir)

    # ------------------------------------------------------------ work
    def _reserve(self, folder: Path, name: str) -> Path:
        """Pick a free file name in ``folder`` and hold it until released."""
        p = PurePosixPath(name)
        stem, suffix = (p.stem, p.suffix) if p.suffix else (name, "")
        with self._cond:
            n = 0
            while True:
                cand = folder / (name if n == 0 else f"{stem} ({n}){suffix}")
                part = cand.with_name(cand.name + core.PART_SUFFIX)
                if cand not in self._reserved and not cand.exists() and not part.exists():
                    self._reserved.add(cand)
                    return cand
                n += 1

    def _release(self, path: Path) -> None:
        with self._cond:
            self._reserved.discard(path)

    def _pick_name(self, job: Download, resp) -> str:
        for candidate in (
            disposition_name(resp.headers.get("Content-Disposition")),
            urlsplit(job.url).path,
            urlsplit(resp.url or "").path,
        ):
            name = safe_name(candidate)
            if name:
                return name
        return f"download-{job.id}"

    def _run(self, job: Download) -> None:
        url, headers = job.auth.apply(job.url)
        req = Request(url, headers={"User-Agent": USER_AGENT})
        for k, v in headers.items():
            req.add_unredirected_header(k, v)  # not re-sent on redirect
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                job.total = int(resp.headers.get("Content-Length") or 0)
                path = self._reserve(Path(job.dest_dir), self._pick_name(job, resp))
                job.filename = path.name
                part = path.with_name(path.name + core.PART_SUFFIX)
                try:
                    with open(part, "wb") as f:
                        while chunk := resp.read(CHUNK):
                            if job.cancel.is_set():
                                raise DownloadCancelled()
                            f.write(chunk)
                            job.done += len(chunk)
                    if job.total and job.done != job.total:
                        raise DownloadError(f"connection closed after {job.done} of {job.total} bytes")
                    os.replace(part, path)
                except BaseException:
                    part.unlink(missing_ok=True)
                    raise
                finally:
                    self._release(path)
        except DownloadCancelled:
            return self._finish(job, CANCELLED, "cancelled; partial file removed")
        except HTTPError as e:
            return self._finish(job, FAILED, f"HTTP {e.code} {e.reason}")
        except URLError as e:
            return self._finish(job, FAILED, f"cannot connect: {e.reason}")
        except DownloadError as e:
            return self._finish(job, FAILED, str(e))
        except (OSError, ValueError) as e:
            return self._finish(job, FAILED, f"{type(e).__name__}: {e}")

        job.output = str(path)
        self._finish(job, DONE, "saved")
        if job.convert and path.suffix.lower() in core.ARCHIVE_EXTS and self.convert_queue:
            try:
                added, _ = self.convert_queue.add(
                    [path], job.quality, delete_original=job.delete_original, recursive=False
                )
                if added:
                    job.convert_job_id = added[0].id
                    job.message = "saved; queued for conversion"
            except (OSError, ValueError) as e:
                job.message = f"saved; could not queue for conversion: {e}"
