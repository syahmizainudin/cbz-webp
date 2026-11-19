"""Tests for watch folders: what is queued, when, and that nothing is queued twice."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from helpers import comic_entries, make_zip

from cbzwebp.jobqueue import DONE, JobQueue
from cbzwebp.watch import Watcher


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "drop"
        self.dir.mkdir()
        self.q = JobQueue(workers=1, image_threads=1).start()
        self.w = Watcher(self.q, [self.dir], 80)

    def tearDown(self):
        self.w.stop()
        self.q.shutdown()
        self.tmp.cleanup()

    def settle(self):
        self.q.wait_idle()

    def test_new_archive_is_queued_once_stable_and_kept(self):
        src = self.dir / "sub" / "a.zip"
        src.parent.mkdir()
        make_zip(src, comic_entries())
        self.assertEqual(self.w.check(), [])  # first sight: maybe still being copied
        self.assertEqual(self.w.check(), [src.resolve()])  # unchanged: queue it
        self.settle()
        (job,) = self.q.jobs()
        self.assertEqual(job.state, DONE, job.message)
        self.assertFalse(job.delete_original)
        self.assertTrue(src.exists())  # watched originals are kept by default
        for _ in range(3):  # neither the original nor the new .cbz is queued again
            self.assertEqual(self.w.check(), [])
        self.assertEqual(len(self.q.jobs()), 1)

    def test_a_growing_file_waits(self):
        src = self.dir / "b.zip"
        make_zip(src, comic_entries())
        self.w.check()
        with open(src, "ab") as f:  # still being written
            f.write(b"x")
        self.assertEqual(self.w.check(), [])
        self.assertEqual(self.w.check(), [src.resolve()])

    def test_failed_file_is_retried_only_after_it_changes(self):
        bad = self.dir / "bad.zip"
        bad.write_bytes(b"not a zip")
        self.w.check()
        self.assertEqual(self.w.check(), [bad.resolve()])
        self.settle()
        self.assertEqual(self.q.jobs()[0].state, "failed")
        self.assertEqual(self.w.check(), [])
        self.assertEqual(self.w.check(), [])
        make_zip(bad, comic_entries())  # replaced with a good one
        st = bad.stat()
        os.utime(bad, (st.st_atime, st.st_mtime + 5))
        self.w.check()
        self.assertEqual(self.w.check(), [bad.resolve()])

    def test_delete_original_when_asked(self):
        w = Watcher(self.q, [self.dir], 80, delete_original=True)
        src = self.dir / "c.zip"
        make_zip(src, comic_entries())
        w.check()
        w.check()
        self.settle()
        self.assertFalse(src.exists())
        self.assertTrue((self.dir / "c.cbz").exists())

    def test_bad_settings(self):
        with self.assertRaises(ValueError):
            Watcher(self.q, [self.dir / "nope"], 80)
        with self.assertRaises(ValueError):
            Watcher(self.q, [self.dir], 0)
        with self.assertRaises(ValueError):
            Watcher(self.q, [self.dir], 80, poll=0)

    def test_runs_in_the_background(self):
        w = Watcher(self.q, [self.dir], 80, poll=0.05).start()
        self.addCleanup(w.stop)
        make_zip(self.dir / "d.zip", comic_entries())
        deadline = time.monotonic() + 10
        while not self.q.jobs() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.q.jobs()), 1)


if __name__ == "__main__":
    unittest.main()
