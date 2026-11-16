"""In-memory job queue: archive-level worker threads + one shared image-encoding pool."""

from __future__ import annotations

import itertools
import os
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Iterable

from . import core

QUEUED, RUNNING, DONE, SKIPPED, FAILED, CANCELLED = (
    "queued",
    "running",
    "done",
    "skipped",
    "failed",
    "cancelled",
)
ACTIVE = {QUEUED, RUNNING}
FINISHED = {DONE, SKIPPED, FAILED, CANCELLED}


def _dest(path: str) -> str:
    return str(Path(path).with_suffix(".cbz")).lower()


@dataclass
class Job:
    id: int
    path: str
    quality: int
    delete_original: bool
    overwrite: bool
    state: str = QUEUED
    done: int = 0
    total: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    message: str = ""
    output: str | None = None
    deleted: str | None = None
    kept: list[str] = field(default_factory=list)
    added_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "cancel"}
        d["name"] = Path(self.path).name
        return d


class JobQueue:
    def __init__(
        self,
        workers: int = 1,
        image_threads: int | None = None,
        on_change: Callable[[Job], None] | None = None,
    ):
        self.workers = max(1, workers)
        self.image_threads = max(1, image_threads or os.cpu_count() or 2)
        self.pool = ThreadPoolExecutor(self.image_threads, thread_name_prefix="webp")
        self.on_change = on_change
        self._jobs: dict[int, Job] = {}
        self._pending: deque[int] = deque()
        self._ids = itertools.count(1)
        self._cond = threading.Condition()
        self._paused = False
        self._stopping = False
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------ lifecycle
    def start(self) -> JobQueue:
        for n in range(self.workers):
            t = threading.Thread(target=self._worker, name=f"archive-{n}", daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def shutdown(self, cancel_running: bool = True) -> None:
        with self._cond:
            self._stopping = True
            for jid in list(self._pending):
                self._finish(self._jobs[jid], CANCELLED, "queue stopped")
            self._pending.clear()
            if cancel_running:
                for j in self._jobs.values():
                    if j.state == RUNNING:
                        j.cancel.set()
            self._cond.notify_all()
        for t in self._threads:
            t.join()
        self.pool.shutdown(wait=True, cancel_futures=True)

    # ------------------------------------------------------------ commands
    def add(
        self,
        paths: Iterable[str | Path],
        quality: int,
        *,
        delete_original: bool = True,
        overwrite: bool = False,
        recursive: bool = True,
    ) -> tuple[list[Job], int]:
        """Scan paths and enqueue each archive not already queued/running. Returns (added, dupes)."""
        if not 1 <= int(quality) <= 100:
            raise ValueError("quality must be 1-100")
        archives = core.scan(paths, recursive=recursive)
        added, dupes = [], 0
        with self._cond:
            active = {j.path for j in self._jobs.values() if j.state in ACTIVE}
            for p in archives:
                if str(p) in active:
                    dupes += 1
                    continue
                job = Job(next(self._ids), str(p), int(quality), delete_original, overwrite)
                self._jobs[job.id] = job
                self._pending.append(job.id)
                added.append(job)
            self._cond.notify_all()
        for j in added:
            self._emit(j)
        return added, dupes

    def cancel(self, job_id: int) -> bool:
        with self._cond:
            job = self._jobs.get(job_id)
            if not job or job.state not in ACTIVE:
                return False
            if job.state == QUEUED:
                self._pending.remove(job_id)
                self._finish(job, CANCELLED, "cancelled before start")
            else:
                job.cancel.set()
                job.message = "cancelling…"
        self._emit(job)
        return True

    def cancel_all(self) -> int:
        with self._cond:
            ids = [j.id for j in self._jobs.values() if j.state in ACTIVE]
        return sum(self.cancel(i) for i in ids)

    def retry(self, job_id: int) -> bool:
        with self._cond:
            job = self._jobs.get(job_id)
            if not job or job.state not in (FAILED, CANCELLED):
                return False
            if any(j.path == job.path and j.state in ACTIVE for j in self._jobs.values()):
                return False
            fresh = Job(job.id, job.path, job.quality, job.delete_original, job.overwrite)
            self._jobs[job.id] = fresh
            self._pending.append(job.id)
            self._cond.notify_all()
        self._emit(fresh)
        return True

    def clear_finished(self) -> int:
        with self._cond:
            gone = [i for i, j in self._jobs.items() if j.state in FINISHED]
            for i in gone:
                del self._jobs[i]
        return len(gone)

    def set_paused(self, paused: bool) -> None:
        with self._cond:
            self._paused = paused
            self._cond.notify_all()

    # ------------------------------------------------------------ queries
    @property
    def paused(self) -> bool:
        return self._paused

    def snapshot(self) -> list[dict]:
        with self._cond:
            return [j.to_dict() for j in self._jobs.values()]

    def jobs(self) -> list[Job]:
        with self._cond:
            return list(self._jobs.values())

    def wait_idle(self, poll: float = 0.2) -> None:
        with self._cond:
            while self._pending or any(j.state == RUNNING for j in self._jobs.values()):
                self._cond.wait(poll)

    # ------------------------------------------------------------ internals
    def _emit(self, job: Job) -> None:
        if self.on_change:
            try:
                self.on_change(job)
            except Exception:  # noqa: BLE001 - a broken listener must not kill a worker
                traceback.print_exc()

    def _finish(self, job: Job, state: str, message: str) -> None:
        job.state, job.message, job.finished_at = state, message, time.time()

    def _next_runnable(self) -> Job | None:
        """First pending job whose output file no running job is writing.

        ``Name.zip`` and ``Name.cbz`` both write ``Name.cbz(.part)``; running them at once
        would corrupt the output. Call with the lock held.
        """
        busy = {_dest(j.path) for j in self._jobs.values() if j.state == RUNNING}
        for jid in self._pending:
            if _dest(self._jobs[jid].path) not in busy:
                self._pending.remove(jid)
                return self._jobs[jid]
        return None

    def _worker(self) -> None:
        while True:
            with self._cond:
                job = None
                while not self._stopping:
                    if not self._paused and (job := self._next_runnable()):
                        break
                    self._cond.wait()
                if self._stopping:
                    return
                job.state, job.started_at, job.message = RUNNING, time.time(), ""
            self._emit(job)
            self._run(job)
            with self._cond:
                self._cond.notify_all()
            self._emit(job)

    def _run(self, job: Job) -> None:
        def progress(done: int, total: int) -> None:
            job.done, job.total = done, total

        try:
            res = core.convert_archive(
                Path(job.path),
                job.quality,
                pool=self.pool,
                delete_original=job.delete_original,
                overwrite=job.overwrite,
                progress=progress,
                cancel=job.cancel,
            )
        except core.Cancelled:
            self._finish(job, CANCELLED, "cancelled; original untouched")
        except core.ConvertError as e:
            self._finish(job, FAILED, f"{e}; original untouched")
        except Exception as e:  # noqa: BLE001
            self._finish(job, FAILED, f"{type(e).__name__}: {e}; original untouched")
        else:
            job.bytes_in, job.bytes_out = res.bytes_in, res.bytes_out
            job.output, job.deleted, job.kept = res.output, res.deleted, res.kept
            self._finish(job, DONE if res.status == "done" else SKIPPED, res.message)
