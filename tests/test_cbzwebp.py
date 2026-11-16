"""Run: python3 -m unittest discover -s tests  (from the cbz-webp folder)."""

import io
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from helpers import comic_entries, img_bytes, make_zip
from PIL import Image

from cbzwebp import core
from cbzwebp.jobqueue import CANCELLED, DONE, FAILED, SKIPPED, JobQueue


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.pool = ThreadPoolExecutor(4)

    def tearDown(self):
        self.pool.shutdown()
        self.tmp.cleanup()

    def comic(self, name="Vol 01.zip", sub=""):
        d = self.dir / sub
        d.mkdir(parents=True, exist_ok=True)
        return make_zip(d / name, comic_entries())


class ConvertTests(Base):
    def test_zip_converted_verified_and_deleted(self):
        src = self.comic()
        res = core.convert_archive(src, 75, pool=self.pool)
        dest = src.with_suffix(".cbz")
        self.assertEqual(res.status, "done")
        self.assertFalse(src.exists(), "original zip should be deleted")
        self.assertEqual(res.deleted, str(src))
        with zipfile.ZipFile(dest) as z:
            names = z.namelist()
            self.assertEqual(
                names,
                ["001.webp", "002.webp", "003.webp", "001.png.webp", "004.webp", "005.gif", "ComicInfo.xml"],
            )
            self.assertEqual(Image.open(io.BytesIO(z.read("002.webp"))).mode, "RGBA")
            self.assertEqual(z.read("ComicInfo.xml"), b"<ComicInfo><Title>x</Title></ComicInfo>")
        self.assertEqual(res.converted, 4)
        self.assertEqual(len(res.kept), 1)  # animated gif
        self.assertFalse(list(self.dir.glob("*.part")))

    def test_keep_original(self):
        src = self.comic()
        core.convert_archive(src, 75, pool=self.pool, delete_original=False)
        self.assertTrue(src.exists())
        self.assertTrue(src.with_suffix(".cbz").exists())

    def test_existing_cbz_conflict_skips_and_keeps_both(self):
        src = self.comic()
        dest = src.with_suffix(".cbz")
        dest.write_bytes(b"existing")
        res = core.convert_archive(src, 75, pool=self.pool)
        self.assertEqual(res.status, "skipped")
        self.assertTrue(src.exists())
        self.assertEqual(dest.read_bytes(), b"existing")

    def test_cbz_recompressed_in_place(self):
        src = self.comic(name="Vol 02.cbz")
        res = core.convert_archive(src, 75, pool=self.pool)
        self.assertEqual(res.status, "done")
        self.assertIsNone(res.deleted)
        with zipfile.ZipFile(src) as z:
            self.assertIn("001.webp", z.namelist())

    def test_all_webp_cbz_skipped_untouched(self):
        src = make_zip(self.dir / "w.cbz", {"1.webp": img_bytes("WEBP")})
        before = src.read_bytes()
        res = core.convert_archive(src, 75, pool=self.pool)
        self.assertEqual(res.status, "skipped")
        self.assertEqual(src.read_bytes(), before)

    def test_no_images_skipped_not_deleted(self):
        src = make_zip(self.dir / "docs.zip", {"readme.txt": b"hi"})
        res = core.convert_archive(src, 75, pool=self.pool)
        self.assertEqual(res.status, "skipped")
        self.assertTrue(src.exists())
        self.assertFalse(src.with_suffix(".cbz").exists())

    def test_corrupt_image_fails_and_keeps_original(self):
        src = make_zip(self.dir / "bad.zip", {"1.jpg": img_bytes("JPEG"), "2.jpg": b"not an image"})
        with self.assertRaises(core.ConvertError):
            core.convert_archive(src, 75, pool=self.pool)
        self.assertTrue(src.exists())
        self.assertFalse(src.with_suffix(".cbz").exists())
        self.assertFalse(list(self.dir.glob("*.part")))

    def test_not_a_zip_fails(self):
        src = self.dir / "fake.zip"
        src.write_bytes(b"nope")
        with self.assertRaises(core.ConvertError):
            core.convert_archive(src, 75, pool=self.pool)
        self.assertTrue(src.exists())

    def test_cancel_leaves_no_part_and_keeps_original(self):
        src = make_zip(self.dir / "big.zip", {f"{i:03}.jpg": img_bytes("JPEG") for i in range(30)})
        cancel = threading.Event()

        def progress(done, total):
            if done >= 3:
                cancel.set()

        with self.assertRaises(core.Cancelled):
            core.convert_archive(src, 75, pool=self.pool, progress=progress, cancel=cancel)
        self.assertTrue(src.exists())
        self.assertFalse(src.with_suffix(".cbz").exists())
        self.assertFalse(list(self.dir.glob("*.part")))

    def test_scan_recursive_and_windows_path(self):
        self.comic(sub="a/b")
        make_zip(self.dir / "x.cbz", {"1.webp": img_bytes("WEBP")})
        (self.dir / "note.txt").write_text("x")
        found = core.scan([self.dir])
        self.assertEqual(len(found), 2)
        self.assertEqual(len(core.scan([self.dir], recursive=False)), 1)
        self.assertEqual(str(core.normalize_path(r"C:\Comics\A")), "/mnt/c/Comics/A")


class QueueTests(Base):
    def test_queue_runs_all_and_dedups(self):
        self.comic("A.zip")
        self.comic("B.zip")
        make_zip(self.dir / "bad.zip", {"1.jpg": b"broken"})
        make_zip(self.dir / "docs.zip", {"a.txt": b"x"})
        q = JobQueue(workers=2, image_threads=2)
        q.set_paused(True)
        q.start()
        added, dupes = q.add([self.dir], 80)
        self.assertEqual(len(added), 4)
        _, dupes = q.add([self.dir], 80)
        self.assertEqual(dupes, 4)
        q.set_paused(False)
        q.wait_idle()
        q.shutdown()
        states = {Path(j.path).name: j.state for j in q.jobs()}
        self.assertEqual(states, {"A.zip": DONE, "B.zip": DONE, "bad.zip": FAILED, "docs.zip": SKIPPED})
        self.assertTrue((self.dir / "bad.zip").exists())
        self.assertFalse((self.dir / "A.zip").exists())

    def test_cancel_queued_and_retry(self):
        self.comic("A.zip")
        q = JobQueue(workers=1, image_threads=1)
        q.set_paused(True)
        q.start()
        (job,), _ = q.add([self.dir], 80, delete_original=False)
        self.assertTrue(q.cancel(job.id))
        self.assertEqual(q.jobs()[0].state, CANCELLED)
        self.assertTrue(q.retry(job.id))
        q.set_paused(False)
        q.wait_idle()
        q.shutdown()
        self.assertEqual(q.jobs()[0].state, DONE)
        self.assertTrue((self.dir / "A.zip").exists())

    def test_same_output_never_runs_twice_at_once(self):
        self.comic("A.zip")
        make_zip(self.dir / "A.cbz", {"1.png": img_bytes("PNG")})
        q = JobQueue(workers=2, image_threads=2)
        q.set_paused(True)
        q.start()
        q.add([self.dir], 80, overwrite=True)
        q.set_paused(False)
        q.wait_idle()
        q.shutdown()
        jobs = sorted(q.jobs(), key=lambda j: j.started_at)
        self.assertEqual([j.state for j in jobs], [DONE, DONE])
        self.assertLessEqual(jobs[0].finished_at, jobs[1].started_at)


if __name__ == "__main__":
    unittest.main()
