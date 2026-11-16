"""Tests for the `convert` command and the web console's HTTP API."""

import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from helpers import comic_entries, img_bytes, make_zip

from cbzwebp import __version__, cli
from cbzwebp.jobqueue import CANCELLED, DONE, FAILED, JobQueue
from cbzwebp.web import serve


def run_cli(*argv):
    """Run the CLI in-process; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:  # argparse errors and --version
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def files(self):
        return sorted(str(p.relative_to(self.dir)) for p in self.dir.rglob("*") if p.is_file())


class CliTests(TmpDir):
    def test_convert_deletes_zip_and_reports(self):
        make_zip(self.dir / "A.zip", comic_entries())
        (self.dir / "sub").mkdir()
        make_zip(self.dir / "sub" / "B.zip", {"1.jpg": img_bytes("JPEG")})
        code, out, _ = run_cli("convert", str(self.dir), "-q", "70", "-t", "2")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files(), ["A.cbz", "sub/B.cbz"])
        self.assertIn("2 converted, 0 skipped, 0 failed, 0 cancelled", out)
        self.assertIn("deleted", out)

    def test_keep_flag_keeps_original(self):
        make_zip(self.dir / "A.zip", comic_entries())
        code, _, _ = run_cli("convert", str(self.dir), "--keep")
        self.assertEqual(code, 0)
        self.assertEqual(self.files(), ["A.cbz", "A.zip"])

    def test_dry_run_changes_nothing(self):
        make_zip(self.dir / "A.zip", comic_entries())
        before = (self.dir / "A.zip").read_bytes()
        code, out, _ = run_cli("convert", str(self.dir), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("1 archive(s) would be queued", out)
        self.assertEqual(self.files(), ["A.zip"])
        self.assertEqual((self.dir / "A.zip").read_bytes(), before)

    def test_no_recursive(self):
        (self.dir / "sub").mkdir()
        make_zip(self.dir / "sub" / "B.zip", {"1.jpg": img_bytes("JPEG")})
        code, out, _ = run_cli("convert", str(self.dir), "--no-recursive")
        self.assertEqual(code, 0)
        self.assertIn("No .zip or .cbz files found", out)

    def test_failure_exit_code_and_original_kept(self):
        make_zip(self.dir / "bad.zip", {"1.jpg": b"broken"})
        code, out, _ = run_cli("convert", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("[FAIL 1/1]", out)
        self.assertEqual(self.files(), ["bad.zip"])

    def test_overwrite_flag(self):
        make_zip(self.dir / "A.zip", comic_entries())
        make_zip(self.dir / "A.cbz", {"old.webp": img_bytes("WEBP")})
        old = (self.dir / "A.cbz").read_bytes()
        code, out, _ = run_cli("convert", str(self.dir))
        self.assertIn("already exists", out)
        self.assertEqual((self.dir / "A.cbz").read_bytes(), old)
        # -j 2: A.cbz and A.zip share an output file, so the queue must not run them together.
        code, out, _ = run_cli("convert", str(self.dir), "--overwrite", "-j", "2")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files(), ["A.cbz"])
        with zipfile.ZipFile(self.dir / "A.cbz") as z:
            self.assertIsNone(z.testzip())
            self.assertIn("001.webp", z.namelist())

    def test_bad_arguments(self):
        self.assertEqual(run_cli("convert", str(self.dir / "missing"))[0], 2)
        self.assertEqual(run_cli("convert", str(self.dir), "-q", "0")[0], 2)
        self.assertEqual(run_cli("convert", str(self.dir), "-q", "101")[0], 2)
        self.assertEqual(run_cli("convert", str(self.dir), "-j", "0")[0], 2)

    def test_version(self):
        code, out, _ = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)


class WebTests(TmpDir):
    def setUp(self):
        super().setUp()
        self.queue = JobQueue(workers=1, image_threads=2).start()
        self.server = serve(self.queue, 0, {"quality": 80})
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.queue.shutdown()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Content-Type": "application/json"} if body is not None else {}
        hdrs.update(headers or {})
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, raw

    def post(self, path, body=None, **kw):
        return self.request("POST", path, {} if body is None else body, **kw)

    def test_page_and_state(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>CBZ WebP Queue</title>", page)
        status, st = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(st["jobs"], [])
        self.assertEqual(st["defaults"]["quality"], 80)
        self.assertEqual(self.request("GET", "/nope")[0], 404)

    def test_add_runs_job_and_dedups(self):
        make_zip(self.dir / "A.zip", comic_entries())
        self.post("/api/pause", {"paused": True})
        status, r = self.post("/api/add", {"paths": [str(self.dir)], "quality": 60})
        self.assertEqual((status, r), (200, {"added": 1, "duplicates": 0}))
        self.assertEqual(self.post("/api/add", {"paths": [str(self.dir)]})[1], {"added": 0, "duplicates": 1})
        self.post("/api/pause", {"paused": False})
        self.queue.wait_idle()
        job = self.request("GET", "/api/state")[1]["jobs"][0]
        self.assertEqual((job["state"], job["quality"], job["name"]), (DONE, 60, "A.zip"))
        self.assertGreater(job["bytes_out"], 0)
        self.assertEqual(self.files(), ["A.cbz"])

    def test_add_validation(self):
        self.assertEqual(self.post("/api/add", {"paths": []})[0], 400)
        self.assertEqual(self.post("/api/add", {"paths": [str(self.dir / "missing")]})[0], 400)
        self.assertEqual(self.post("/api/add", {"paths": [str(self.dir)], "quality": 0})[0], 400)
        self.assertEqual(self.post("/api/add", {"paths": [str(self.dir)], "quality": "x"})[0], 400)
        self.assertEqual(self.post("/api/nope")[0], 404)

    def test_cancel_retry_clear(self):
        make_zip(self.dir / "A.zip", comic_entries())
        self.post("/api/pause", {"paused": True})
        self.post("/api/add", {"paths": [str(self.dir)], "delete_original": False})
        job_id = self.request("GET", "/api/state")[1]["jobs"][0]["id"]
        self.assertEqual(self.post("/api/cancel", {"id": job_id})[1], {"ok": True})
        self.assertEqual(self.request("GET", "/api/state")[1]["jobs"][0]["state"], CANCELLED)
        self.assertEqual(self.post("/api/retry", {"id": job_id})[1], {"ok": True})
        self.post("/api/pause", {"paused": False})
        self.queue.wait_idle()
        self.assertEqual(self.post("/api/clear")[1], {"cleared": 1})
        self.assertEqual(self.request("GET", "/api/state")[1]["jobs"], [])
        self.assertEqual(self.files(), ["A.cbz", "A.zip"])

    def test_cancel_all(self):
        make_zip(self.dir / "A.zip", comic_entries())
        make_zip(self.dir / "B.zip", comic_entries())
        self.post("/api/pause", {"paused": True})
        self.post("/api/add", {"paths": [str(self.dir)]})
        self.assertEqual(self.post("/api/cancel-all")[1], {"cancelled": 2})
        self.assertEqual(self.files(), ["A.zip", "B.zip"])

    def test_failed_job_reported(self):
        make_zip(self.dir / "bad.zip", {"1.jpg": b"broken"})
        self.post("/api/add", {"paths": [str(self.dir)]})
        self.queue.wait_idle()
        job = self.request("GET", "/api/state")[1]["jobs"][0]
        self.assertEqual(job["state"], FAILED)
        self.assertIn("original untouched", job["message"])
        self.assertEqual(self.files(), ["bad.zip"])

    # The console can delete files, so these guards matter.
    def test_rejects_non_json_post(self):
        status, _ = self.request(
            "POST", "/api/clear", b"a=1", {"Content-Type": "application/x-www-form-urlencoded"}
        )
        self.assertEqual(status, 415)

    def test_rejects_cross_origin(self):
        self.assertEqual(self.post("/api/clear", headers={"Origin": "http://evil.example"})[0], 403)
        same = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.post("/api/clear", headers=same)[0], 200)

    def test_rejects_foreign_host(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.post("/api/clear", headers={"Host": "evil.example"})[0], 403)


if __name__ == "__main__":
    unittest.main()
