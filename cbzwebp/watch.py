"""Watch folders: archives that appear in them are queued for conversion on their own.

Polling with the standard library (no inotify, which WSL's /mnt drives don't deliver anyway):

  * every ``poll`` seconds the folders are scanned (subfolders too) for .zip/.cbz files;
  * a file is queued once its size and modification time are the same on two scans in a row,
    so one that is still being copied or downloaded is left alone;
  * each version of a file (path, size, mtime) is queued once. Whether it converts, fails or is
    skipped, it isn't queued again until it changes;
  * a finished conversion's output is remembered as handled too, so the new .cbz isn't queued.

``delete_original`` defaults to False: nobody clicks anything before a watched file is converted.
What is remembered is kept in memory, so after a restart files still in the folders are queued
once more (a .cbz that is already WebP is skipped quickly).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path

from . import core
from .jobqueue import DONE, FINISHED, JobQueue


class Watcher:
    def __init__(
        self,
        queue: JobQueue,
        folders: Iterable[str | Path],
        quality: int,
        *,
        method: int = core.DEFAULT_METHOD,
        delete_original: bool = False,
        poll: float = 30.0,
    ):
        self.queue = queue
        self.folders = [core.normalize_path(str(f)) for f in folders]
        for f in self.folders:
            if not f.is_dir():
                raise ValueError(f"watch folder does not exist: {f}")
        if not 1 <= int(quality) <= 100 or not 0 <= int(method) <= 6:
            raise ValueError("quality must be 1-100 and method 0-6")
        if poll <= 0:
            raise ValueError("poll must be more than 0 seconds")
        self.quality, self.method = int(quality), int(method)
        self.delete_original = delete_original
        self.poll = poll
        self._last: dict[Path, tuple[int, float]] = {}  # what the previous scan saw
        self._handled: dict[Path, tuple[int, float]] = {}  # versions queued or produced
        self._jobs: dict[int, Path] = {}  # job id -> the file it converts
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> Watcher:
        self._thread = threading.Thread(target=self._loop, name="watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.check()
            except Exception as e:  # noqa: BLE001 - a bad scan must not end the watching
                print(f"cbz-webp: watching {', '.join(map(str, self.folders))} failed once: {e}", flush=True)

    @staticmethod
    def _version(p: Path) -> tuple[int, float] | None:
        try:
            st = p.stat()
        except OSError:
            return None
        return st.st_size, st.st_mtime

    def check(self) -> list[Path]:
        """One scan. Returns the files queued by it."""
        self._note_finished()
        found: list[Path] = []
        for folder in self.folders:
            try:
                found += core.scan([folder])
            except (FileNotFoundError, OSError):
                continue  # a drive that went away; try again next time
        queued_now = []
        seen: dict[Path, tuple[int, float]] = {}
        busy = set(self._jobs.values())
        for f in found:
            version = self._version(f)
            if version is None:
                continue
            seen[f] = version
            if f in busy or self._handled.get(f) == version or self._last.get(f) != version:
                continue  # converting now, done already, or still changing
            added, _ = self.queue.add(
                [f], self.quality, delete_original=self.delete_original, recursive=False, method=self.method
            )
            self._handled[f] = version
            for job in added:
                self._jobs[job.id] = f
            queued_now.append(f)
        self._last = seen
        return queued_now

    def _note_finished(self) -> None:
        jobs = {j.id: j for j in self.queue.jobs()}
        for job_id in list(self._jobs):
            job = jobs.get(job_id)
            if job is not None and job.state not in FINISHED:
                continue
            del self._jobs[job_id]
            if job is not None and job.state == DONE and job.output:
                out = Path(job.output)
                version = self._version(out)
                if version is not None:
                    self._handled[out] = version
