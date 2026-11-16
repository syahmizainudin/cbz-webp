"""In-memory job queues.

``BaseQueue`` holds the pending list, workers, pause, cancel, retry and clear; ``JobQueue``
converts archives (worker threads + one shared image-encoding pool). dl-queue has the same
``BaseQueue`` in ``dlqueue/basequeue.py``; keep the two in step.
"""

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

    def fresh(self) -> Job:
        """A new queued copy of this job, for retry."""
        return Job(self.id, self.path, self.quality, self.delete_original, self.overwrite)


class BaseQueue:
    """Shared queue machinery. Subclasses implement ``_run`` and may override ``_busy_key``.

    Jobs need ``id``, ``state``, ``message``, ``started_at``, ``finished_at``, a ``cancel``
    event, ``to_dict()`` and ``fresh()``.
    """

    thread_prefix = "worker"

    def __init__(self, workers: int = 1, on_change: Callable | None = None):
        self.workers = max(1, workers)
        self.on_change = on_change
        self._jobs: dict = {}
        self._pending: deque[int] = deque()
        self._ids = itertools.count(1)
        self._cond = threading.Condition()
        self._paused = False
        self._stopping = False
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------ lifecycle
    def start(self):
        for n in range(self.workers):
            t = threading.Thread(target=self._worker, name=f"{self.thread_prefix}-{n}", daemon=True)
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
        self._after_shutdown()

    def _after_shutdown(self) -> None:
        pass

    # ------------------------------------------------------------ commands
    def _enqueue(self, jobs: list) -> None:
        """Register new jobs and wake the workers. Call with the lock held."""
        for job in jobs:
            self._jobs[job.id] = job
            self._pending.append(job.id)
        self._cond.notify_all()

    def _new_id(self) -> int:
        return next(self._ids)

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
            key = self._dedup_key(job)
            if any(self._dedup_key(j) == key and j.state in ACTIVE for j in self._jobs.values()):
                return False
            fresh = job.fresh()
            self._enqueue([fresh])
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

    def jobs(self) -> list:
        with self._cond:
            return list(self._jobs.values())

    def wait_idle(self, poll: float = 0.2) -> None:
        with self._cond:
            while self._pending or any(j.state == RUNNING for j in self._jobs.values()):
                self._cond.wait(poll)

    # ------------------------------------------------------------ internals
    def _emit(self, job) -> None:
        if self.on_change:
            try:
                self.on_change(job)
            except Exception:  # noqa: BLE001 - a broken listener must not kill a worker
                traceback.print_exc()

    def _finish(self, job, state: str, message: str) -> None:
        job.state, job.message, job.finished_at = state, message, time.time()

    def _dedup_key(self, job):
        """Jobs with equal keys are never queued or running twice."""
        return job.id

    def _busy_key(self, job):
        """Jobs with equal non-None keys never run at the same time."""
        return None

    def _next_runnable(self):
        """First pending job whose busy key no running job holds. Call with the lock held."""
        busy = {self._busy_key(j) for j in self._jobs.values() if j.state == RUNNING} - {None}
        for jid in self._pending:
            if self._busy_key(self._jobs[jid]) not in busy:
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

    def _run(self, job) -> None:
        raise NotImplementedError


class JobQueue(BaseQueue):
    """Converts archives. Image encoding for all jobs shares one thread pool."""

    thread_prefix = "archive"

    def __init__(
        self,
        workers: int = 1,
        image_threads: int | None = None,
        on_change: Callable[[Job], None] | None = None,
    ):
        super().__init__(workers, on_change)
        self.image_threads = max(1, image_threads or os.cpu_count() or 2)
        self.pool = ThreadPoolExecutor(self.image_threads, thread_name_prefix="webp")

    def _after_shutdown(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)

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
                added.append(Job(self._new_id(), str(p), int(quality), delete_original, overwrite))
                active.add(str(p))
            self._enqueue(added)
        for j in added:
            self._emit(j)
        return added, dupes

    def _dedup_key(self, job: Job):
        return job.path

    def _busy_key(self, job: Job):
        # Name.zip and Name.cbz both write Name.cbz(.part): never run them at once.
        return _dest(job.path)

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
