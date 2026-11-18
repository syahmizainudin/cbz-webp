"""Run: python3 -m unittest discover -s tests  (from the cbz-webp folder)."""

import io
import struct
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from helpers import comic_entries, img_bytes, make_zip
from PIL import Image

from cbzwebp import core
from cbzwebp.jobqueue import CANCELLED, DONE, FAILED, RUNNING, SKIPPED, BaseQueue, Job, JobQueue


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

    def _corrupt_part_then_verify(self, how):
        """Patch ``core._verify`` to damage the .part on disk before the real check runs."""
        real = core._verify

        def wrapper(part, expected, webp_names, **kw):
            with zipfile.ZipFile(part) as z:
                info = z.getinfo(sorted(webp_names)[0])
            with open(part, "r+b") as f:
                f.seek(info.header_offset)
                head = f.read(30)
                fn_len, extra_len = struct.unpack("<HH", head[26:30])
                start = info.header_offset + 30 + fn_len + extra_len
                how(f, start, info)
            return real(part, expected, webp_names, **kw)

        return mock.patch.object(core, "_verify", wrapper)

    def _run_failing_job(self, src, how):
        with self._corrupt_part_then_verify(how):
            q = JobQueue(workers=1, image_threads=2).start()
            (job,), _ = q.add([src], 75)
            q.wait_idle()
            q.shutdown()
        self.assertEqual(job.state, FAILED, job.message)
        self.assertIn("verify:", job.message)
        self.assertTrue(src.exists(), "source must survive a failed verify")
        self.assertFalse(src.with_suffix(".cbz").exists())
        self.assertFalse(list(self.dir.glob("*.part")), ".part must be removed")
        return job

    def test_verify_reads_part_from_disk_crc_error_fails(self):
        src = self.comic()

        def flip(f, start, info):  # damaged bytes: the stored CRC no longer matches
            f.seek(start + info.compress_size // 2)
            f.write(b"\xff" * 16)

        job = self._run_failing_job(src, flip)
        self.assertIn("Bad CRC", job.message)

    def test_verify_undecodable_webp_fails(self):
        src = self.comic()

        def garbage(f, start, info):  # valid CRC, but the bytes are not a WebP
            junk = b"\x00" * info.compress_size
            f.seek(start)
            f.write(junk)
            # patch the CRC in the local header and the central directory to match
            crc = struct.pack("<I", zipfile.crc32(junk) & 0xFFFFFFFF)
            f.seek(info.header_offset + 14)
            f.write(crc)
            f.seek(0)
            data = f.read()
            name = info.filename.encode()
            pos = data.find(name, data.find(b"PK\x01\x02"))
            cd = data.rfind(b"PK\x01\x02", 0, pos)
            f.seek(cd + 16)
            f.write(crc)

        job = self._run_failing_job(src, garbage)
        self.assertIn("does not decode", job.message)

    def test_scan_recursive_and_windows_path(self):
        self.comic(sub="a/b")
        make_zip(self.dir / "x.cbz", {"1.webp": img_bytes("WEBP")})
        (self.dir / "note.txt").write_text("x")
        found = core.scan([self.dir])
        self.assertEqual(len(found), 2)
        self.assertEqual(len(core.scan([self.dir], recursive=False)), 1)
        self.assertEqual(str(core.normalize_path(r"C:\Comics\A")), "/mnt/c/Comics/A")

    def test_output_path(self):
        self.assertEqual(core.output_path(Path("/a/Vol 1.zip")), Path("/a/Vol 1.cbz"))
        self.assertEqual(core.output_path(Path("/a/Vol 1.ZIP")), Path("/a/Vol 1.cbz"))
        self.assertEqual(core.output_path(Path("/a/Vol 1.CBZ")), Path("/a/Vol 1.CBZ"))
        self.assertEqual(core.output_path(Path("/a/v1.0.cbz")), Path("/a/v1.0.cbz"))

    def test_scan_dedupes_symlinks_and_skips_folders_named_like_archives(self):
        real = self.comic("A.zip")
        (self.dir / "link.zip").symlink_to(real)
        (self.dir / "folder.cbz").mkdir()
        (self.dir / "dangling.zip").symlink_to(self.dir / "missing.zip")
        self.assertEqual(core.scan([self.dir]), [real.resolve()])

    def test_exif_orientation_applied(self):
        im = Image.new("RGB", (300, 200), "white")
        im.paste((255, 0, 0), (0, 0, 30, 30))  # red mark top-left of the stored pixels
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation: rotate 90 degrees clockwise to display
        b = io.BytesIO()
        im.save(b, "JPEG", quality=95, exif=exif)
        webp, reason = core.encode_webp(b.getvalue(), 90)
        self.assertEqual(reason, "")
        with Image.open(io.BytesIO(webp)) as out:
            self.assertEqual(out.size, (200, 300))
            r, g, _ = out.convert("RGB").getpixel((185, 15))  # the mark is now top-right
            self.assertGreater(r, 200)
            self.assertLess(g, 80)


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

    def test_cancel_all_one_pass_emits_once_per_job(self):
        class Blocking(BaseQueue):
            def _run(self, job):
                job.cancel.wait(10)
                self._finish(job, CANCELLED if job.cancel.is_set() else DONE, "")

        seen = []
        q = Blocking(workers=1, on_change=lambda j: seen.append((j.id, j.state, j.message)))
        jobs = [Job(q._new_id(), f"/x/{i}.zip", 80, False, False) for i in range(3)]
        with q._cond:
            q._enqueue(jobs)
        q.start()
        while jobs[0].state != RUNNING:
            threading.Event().wait(0.01)
        seen.clear()
        self.assertEqual(q.cancel_all(), 3)
        self.assertEqual(
            seen[:3],
            [
                (1, RUNNING, "cancelling…"),
                (2, CANCELLED, "cancelled before start"),
                (3, CANCELLED, "cancelled before start"),
            ],
        )
        self.assertEqual(list(q._pending), [])
        q.wait_idle()
        self.assertEqual(q.cancel_all(), 0)
        q.shutdown()
        self.assertEqual([j.state for j in q.jobs()], [CANCELLED] * 3)
        self.assertEqual([i for i, st, _ in seen if st == CANCELLED], [2, 3, 1])


class MethodTests(Base):
    def test_method_reaches_encoder_and_changes_output(self):
        sizes = {}
        for m in (0, 6):
            src = make_zip(self.dir / f"m{m}.zip", {"1.png": img_bytes("PNG", size=(600, 800))})
            q = JobQueue(workers=1, image_threads=1).start()
            (job,), _ = q.add([src], 80, delete_original=False, method=m)
            q.wait_idle()
            q.shutdown()
            self.assertEqual((job.state, job.method), (DONE, m))
            sizes[m] = job.bytes_out
        self.assertNotEqual(sizes[0], sizes[6])

    def test_method_validated_and_kept_on_retry(self):
        q = JobQueue(workers=1, image_threads=1)
        with self.assertRaisesRegex(ValueError, "method must be 0-6"):
            q.add([self.dir], 80, method=7)
        self.comic("A.zip")
        q.set_paused(True)
        q.start()
        (job,), _ = q.add([self.dir], 80, method=2)
        q.cancel(job.id)
        q.retry(job.id)
        self.assertEqual(q.jobs()[0].method, 2)
        q.shutdown()


if __name__ == "__main__":
    unittest.main()
