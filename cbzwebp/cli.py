"""Command line: ``cbz-webp convert`` (run a queue to completion) and ``cbz-webp ui``."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import webbrowser

from . import __version__, core
from .jobqueue import DONE, FAILED, RUNNING, SKIPPED, Job, JobQueue


def _fmt(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _quality(v: str) -> int:
    q = int(v)
    if not 1 <= q <= 100:
        raise argparse.ArgumentTypeError("quality must be 1-100")
    return q


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-q", "--quality", type=_quality, default=80, help="WebP quality 1-100 (default 80)")
    p.add_argument("-j", "--jobs", type=int, default=1,
                   help="archives converted at the same time (default 1)")
    p.add_argument("-t", "--threads", type=int, default=os.cpu_count(),
                   help=f"image-encoding threads shared by all jobs (default {os.cpu_count()})")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cbz-webp",
        description="Recompress images inside .zip/.cbz archives to WebP and save them as .cbz.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="scan folders, queue every archive, convert, exit")
    c.add_argument("paths", nargs="+", help="folders (scanned recursively) or archive files")
    _common(c)
    c.add_argument("--keep", action="store_true", help="keep the original .zip (default: delete after verify)")
    c.add_argument("--overwrite", action="store_true", help="replace an existing Name.cbz next to Name.zip")
    c.add_argument("--no-recursive", action="store_true", help="only scan the given folders, not subfolders")
    c.add_argument("--dry-run", action="store_true", help="list what would be queued and exit")

    u = sub.add_parser("ui", help="start the local web console")
    _common(u)
    u.add_argument("-p", "--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765)")
    u.add_argument("--open", action="store_true", help="open the console in a browser")
    u.add_argument("paths", nargs="*", help="optional folders to queue at start-up")
    u.add_argument("--keep", action="store_true", help="for start-up paths: keep the original .zip")
    return ap


def cmd_convert(args) -> int:
    try:
        archives = core.scan(args.paths, recursive=not args.no_recursive)
    except FileNotFoundError as e:
        print(f"cbz-webp: {e}", file=sys.stderr)
        return 2
    if not archives:
        print("No .zip or .cbz files found.")
        return 0
    if args.dry_run:
        for a in archives:
            print(a)
        print(f"{len(archives)} archive(s) would be queued at quality {args.quality}"
              f"{'' if args.keep else '; original .zip files deleted after verify'}.")
        return 0

    total = len(archives)
    lock = threading.Lock()
    finished = [0]

    def on_change(job: Job) -> None:
        with lock:
            if job.state == RUNNING:
                if job.message:  # "cancelling…" update, not a start
                    return
                print(f"[start] {job.path}", flush=True)
                return
            if job.state in (DONE, SKIPPED, FAILED, "cancelled"):
                finished[0] += 1
                tag = {DONE: "done", SKIPPED: "skip", FAILED: "FAIL"}.get(job.state, "cancel")
                size = f" {_fmt(job.bytes_in)} -> {_fmt(job.bytes_out)} |" if job.state == DONE else ""
                print(f"[{tag} {finished[0]}/{total}] {job.path}:{size} {job.message}", flush=True)
                for k in job.kept:
                    print(f"        kept as-is: {k}", flush=True)
                if job.deleted:
                    print(f"        deleted {job.deleted}", flush=True)

    q = JobQueue(workers=args.jobs, image_threads=args.threads, on_change=on_change).start()
    q.add(archives, args.quality, delete_original=not args.keep, overwrite=args.overwrite,
          recursive=False)

    def interrupt(*_):
        print("\nCancelling… (originals are left untouched)", flush=True)
        q.cancel_all()

    signal.signal(signal.SIGINT, interrupt)
    q.wait_idle()
    q.shutdown()

    jobs = q.jobs()
    done = [j for j in jobs if j.state == DONE]
    failed = [j for j in jobs if j.state == FAILED]
    bin_, bout = sum(j.bytes_in for j in done), sum(j.bytes_out for j in done)
    print(f"\n{len(done)} converted, {sum(j.state == SKIPPED for j in jobs)} skipped, "
          f"{len(failed)} failed, {sum(j.state == 'cancelled' for j in jobs)} cancelled.")
    if bin_:
        print(f"Size {_fmt(bin_)} -> {_fmt(bout)} ({100 * (1 - bout / bin_):.0f}% smaller).")
    if failed:
        return 1
    return 130 if any(j.state == "cancelled" for j in jobs) else 0


def cmd_ui(args) -> int:
    from .web import serve

    q = JobQueue(workers=args.jobs, image_threads=args.threads).start()
    if args.paths:
        added, _ = q.add(args.paths, args.quality, delete_original=not args.keep)
        print(f"Queued {len(added)} archive(s).")
    try:
        server = serve(q, args.port, {"quality": args.quality})
    except OSError as e:
        print(f"cbz-webp: cannot listen on 127.0.0.1:{args.port}: {e}", file=sys.stderr)
        q.shutdown()
        return 2
    url = f"http://127.0.0.1:{args.port}/"
    print(f"cbz-webp console: {url}  (Ctrl+C to stop; running jobs are cancelled, originals kept)",
          flush=True)

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…", flush=True)
    finally:
        server.server_close()
        q.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.jobs < 1 or (args.threads or 1) < 1:
        print("cbz-webp: --jobs and --threads must be at least 1", file=sys.stderr)
        return 2
    return cmd_convert(args) if args.cmd == "convert" else cmd_ui(args)


if __name__ == "__main__":
    sys.exit(main())
