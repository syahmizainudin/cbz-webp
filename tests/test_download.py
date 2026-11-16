"""Tests for the download queue, against a local HTTP server (never a real URL)."""

import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

from helpers import FileServer, comic_entries, make_zip

from cbzwebp.download import Auth, DownloadQueue
from cbzwebp.jobqueue import CANCELLED, DONE, FAILED, JobQueue

KEY = FileServer.KEY


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.server = FileServer()
        self.queues = []

    def tearDown(self):
        for q in self.queues:
            q.shutdown()
        self.server.close()
        self.tmp.cleanup()

    def queue(self, workers=1, convert_queue=None):
        q = DownloadQueue(workers=workers, convert_queue=convert_queue, timeout=10).start()
        self.queues.append(q)
        return q

    def run_one(self, path, auth=None, **kw):
        q = self.queue()
        (job,), _ = q.add([self.server.base + path], self.dir, auth, convert=False, **kw)
        q.wait_idle()
        return job, q

    def files(self):
        return sorted(p.name for p in self.dir.iterdir())

    def last_request(self, prefix):
        return [r for r in self.server.requests if r["path"].startswith(prefix)][-1]

    # ---------------------------------------------------------- auth modes
    def test_bearer(self):
        job, _ = self.run_one("/secure/a.bin", Auth("bearer", KEY))
        self.assertEqual(job.state, DONE, job.message)
        self.assertEqual((self.dir / "a.bin").read_bytes(), b"data:a.bin")
        self.assertEqual(self.last_request("/secure")["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(self.files(), ["a.bin"])

    def test_custom_header(self):
        job, _ = self.run_one("/secure/a.bin", Auth("header", KEY, "X-API-Key"))
        self.assertEqual(job.state, DONE, job.message)
        self.assertNotIn("Authorization", self.last_request("/secure")["headers"])

    def test_query_param_key_not_stored(self):
        job, _ = self.run_one("/secure/a.bin?x=1", Auth("query", KEY, "api_key"))
        self.assertEqual(job.state, DONE, job.message)
        req = self.last_request("/secure")
        self.assertEqual(req["query"], {"x": ["1"], "api_key": [KEY]})
        self.assertNotIn(KEY, job.url)

    def test_key_never_in_state(self):
        for auth in (Auth("bearer", KEY), Auth("header", KEY, "X-Token"), Auth("query", KEY, "k")):
            _, q = self.run_one("/secure/a.bin", auth)
            self.assertNotIn(KEY, json.dumps(q.snapshot()))
            self.assertNotIn(KEY, repr(q.jobs()))

    def test_wrong_key_fails(self):
        job, _ = self.run_one("/secure/a.bin", Auth("bearer", "wrong"))
        self.assertEqual(job.state, FAILED)
        self.assertIn("HTTP 401", job.message)
        self.assertEqual(self.files(), [])

    def test_404_fails(self):
        job, _ = self.run_one("/missing")
        self.assertEqual(job.state, FAILED)
        self.assertIn("HTTP 404", job.message)

    def test_redirect_does_not_forward_key(self):
        job, _ = self.run_one("/redirect", Auth("bearer", KEY))
        self.assertEqual(job.state, DONE, job.message)
        self.assertEqual(self.last_request("/redirect")["headers"]["Authorization"], f"Bearer {KEY}")
        landing = self.last_request("/landing")["headers"]
        self.assertNotIn("Authorization", landing)
        self.assertEqual((self.dir / "redirect").read_bytes(), b"landed")

    def test_unreachable_server_fails(self):
        q = self.queue()
        (job,), _ = q.add(["http://127.0.0.1:1/x.zip"], self.dir, convert=False)
        q.wait_idle()
        self.assertEqual(job.state, FAILED)
        self.assertIn("cannot connect", job.message)

    # ---------------------------------------------------------- file names
    def test_content_disposition_is_sanitized(self):
        job, _ = self.run_one("/cd?name=../../evil%20name.zip")
        self.assertEqual(job.state, DONE, job.message)
        self.assertEqual(self.files(), ["evil name.zip"])
        self.assertFalse((self.dir.parent / "evil name.zip").exists())

    def test_rfc5987_filename(self):
        job, _ = self.run_one("/cdstar")
        self.assertEqual(job.filename, "Volé 01.zip")

    def test_existing_file_not_overwritten(self):
        (self.dir / "a.bin").write_bytes(b"mine")
        job, _ = self.run_one("/files/x/a.bin")
        self.assertEqual(job.filename, "a (1).bin")
        self.assertEqual((self.dir / "a.bin").read_bytes(), b"mine")

    def test_concurrent_same_name_get_distinct_files(self):
        q = self.queue(workers=3)
        urls = [f"{self.server.base}/files/{d}/same.bin" for d in "abc"]
        q.add(urls, self.dir, convert=False)
        q.wait_idle()
        self.assertEqual(self.files(), ["same (1).bin", "same (2).bin", "same.bin"])

    # ---------------------------------------------------------- partial data
    def test_no_content_length(self):
        job, _ = self.run_one("/nolength")
        self.assertEqual(job.state, DONE, job.message)
        self.assertEqual(job.total, 0)
        self.assertEqual((self.dir / "nolength").read_bytes(), b"no length here")

    def test_short_read_fails_and_cleans_up(self):
        job, _ = self.run_one("/short")
        self.assertEqual(job.state, FAILED)
        self.assertEqual(self.files(), [])

    def test_cancel_mid_download_cleans_up(self):
        q = self.queue()
        (job,), _ = q.add([self.server.base + "/slow"], self.dir, convert=False)
        deadline = time.time() + 10
        while job.done < 256 * 1024 and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(q.cancel(job.id))
        q.wait_idle()
        self.assertEqual(job.state, CANCELLED)
        self.assertEqual(self.files(), [])

    def test_retry_after_failure(self):
        job, q = self.run_one("/secure/a.bin", Auth("bearer", "wrong"))
        self.assertTrue(q.retry(job.id))
        q.wait_idle()
        self.assertEqual(q.jobs()[0].state, FAILED)  # same key, same result
        self.assertFalse(q.retry(999))

    # ---------------------------------------------------------- convert hand-off
    def test_auto_convert(self):
        src = make_zip(self.dir / "src.zip", comic_entries())
        self.server.files["Ch 1.zip"] = src.read_bytes()
        src.unlink()
        out = self.dir / "out"
        out.mkdir()
        cq = JobQueue(workers=1, image_threads=2).start()
        self.queues.append(cq)
        q = self.queue(convert_queue=cq)
        (job,), _ = q.add([self.server.base + "/files/x/Ch%201.zip"], out, quality=70)
        q.wait_idle()
        self.assertEqual(job.state, DONE, job.message)
        self.assertIsNotNone(job.convert_job_id)
        cq.wait_idle()
        conv = cq.jobs()[0]
        self.assertEqual((conv.id, conv.state, conv.quality), (job.convert_job_id, DONE, 70))
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["Ch 1.cbz"])
        with zipfile.ZipFile(out / "Ch 1.cbz") as z:
            self.assertIn("001.webp", z.namelist())

    def test_convert_off_or_not_archive(self):
        cq = JobQueue(workers=1, image_threads=1).start()
        self.queues.append(cq)
        q = self.queue(convert_queue=cq)
        q.add([self.server.base + "/files/x/a.zip"], self.dir, convert=False)
        q.add([self.server.base + "/files/x/b.bin"], self.dir, convert=True)
        q.wait_idle()
        self.assertEqual(cq.jobs(), [])
        self.assertEqual(self.files(), ["a.zip", "b.bin"])

    # ---------------------------------------------------------- validation
    def test_validation(self):
        q = self.queue()
        bad = [
            (["ftp://x/y.zip"], self.dir, None),
            (["file:///etc/passwd"], self.dir, None),
            (["not a url"], self.dir, None),
            ([], self.dir, None),
            (["http://x/y"], self.dir / "missing", None),
            (["http://x/y"], "", None),
            (["http://x/y"], "  ", None),
            (["http://x/y"], self.dir, Auth("bearer", "")),
            (["http://x/y"], self.dir, Auth("bearer", "a\r\nX-Evil: 1")),
            (["http://x/y"], self.dir, Auth("header", KEY, "Bad:Name")),
            (["http://x/y"], self.dir, Auth("header", KEY, "")),
            (["http://x/y"], self.dir, Auth("query", KEY, "a b")),
            (["http://x/y"], self.dir, Auth("basic", KEY)),
        ]
        for urls, dest, auth in bad:
            with self.subTest(urls=urls, dest=dest, auth=auth), self.assertRaises(ValueError):
                q.add(urls, dest, auth)
        with self.assertRaises(ValueError):
            q.add(["http://x/y"], self.dir, quality=0)
        self.assertEqual(q.jobs(), [])

    def test_dedup_same_url_same_folder(self):
        q = self.queue()
        q.set_paused(True)
        url = self.server.base + "/files/x/a.bin"
        self.assertEqual(len(q.add([url, url], self.dir, convert=False)[0]), 1)
        self.assertEqual(q.add([url], self.dir, convert=False)[1], 1)
        other = self.dir / "o"
        other.mkdir()
        self.assertEqual(len(q.add([url], other, convert=False)[0]), 1)

    def test_pause_holds_work(self):
        q = self.queue()
        q.set_paused(True)
        (job,), _ = q.add([self.server.base + "/files/x/a.bin"], self.dir, convert=False)
        time.sleep(0.3)
        self.assertEqual(job.state, "queued")
        q.set_paused(False)
        q.wait_idle()
        self.assertEqual(job.state, DONE)

    def test_shutdown_cancels_running(self):
        q = DownloadQueue(workers=1, timeout=10).start()
        (job,), _ = q.add([self.server.base + "/slow"], self.dir, convert=False)
        while job.done == 0:
            time.sleep(0.05)
        t = threading.Thread(target=q.shutdown)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(job.state, CANCELLED)
        self.assertEqual(self.files(), [])


if __name__ == "__main__":
    unittest.main()
