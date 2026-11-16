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
from urllib.parse import quote

from helpers import comic_entries, img_bytes, make_zip

from cbzwebp import __version__, cli
from cbzwebp.jobqueue import CANCELLED, DONE, FAILED, JobQueue
from cbzwebp.web import ConvertAPI, UI_HTML, current_user, serve, serve_convert


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

    def test_method_flag(self):
        make_zip(self.dir / "A.zip", comic_entries())
        self.assertEqual(run_cli("convert", str(self.dir), "-m", "1")[0], 0)
        self.assertEqual(run_cli("convert", str(self.dir), "-m", "7")[0], 2)

    def test_bad_arguments(self):
        self.assertEqual(run_cli("convert", str(self.dir / "missing"))[0], 2)
        self.assertEqual(run_cli("convert", str(self.dir), "-q", "0")[0], 2)
        self.assertEqual(run_cli("convert", str(self.dir), "-q", "101")[0], 2)
        self.assertEqual(run_cli("convert", str(self.dir), "-j", "0")[0], 2)

    def test_version(self):
        code, out, _ = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)


class WebBase(TmpDir):
    """Starts the console server on a free port."""

    def setUp(self):
        super().setUp()
        self.queue = JobQueue(workers=1, image_threads=2).start()
        self.server = serve_convert(0, self.queue, {"quality": 80})
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


class WebTests(WebBase):
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
        status, r = self.post("/api/add", {"paths": [str(self.dir)], "quality": 60, "method": 2})
        self.assertEqual((status, r), (200, {"added": 1, "duplicates": 0}))
        self.assertEqual(self.post("/api/add", {"paths": [str(self.dir)]})[1], {"added": 0, "duplicates": 1})
        self.post("/api/pause", {"paused": False})
        self.queue.wait_idle()
        job = self.request("GET", "/api/state")[1]["jobs"][0]
        self.assertEqual((job["state"], job["quality"], job["method"], job["name"]), (DONE, 60, 2, "A.zip"))
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


class WebFolderTests(WebBase):
    """The folder picker endpoint."""

    def test_dirs_lists_folders_only(self):
        for d in ("B", "a", ".hidden"):
            (self.dir / d).mkdir()
        make_zip(self.dir / "x.zip", {"1.txt": b"x"})
        (self.dir / "y.cbz").write_bytes(b"")
        (self.dir / "note.txt").write_text("x")
        status, r = self.request("GET", "/api/dirs?path=" + quote(str(self.dir)))
        self.assertEqual(status, 200, r)
        self.assertEqual(r["path"], str(self.dir.resolve()))
        self.assertEqual(r["dirs"], ["a", "B"])
        self.assertEqual(r["files"], 2)
        self.assertEqual(r["parent"], str(self.dir.resolve().parent))
        self.assertEqual(r["shortcuts"][0]["label"], "Home")

    def test_dirs_defaults_to_home_and_rejects_bad_paths(self):
        status, r = self.request("GET", "/api/dirs")
        self.assertEqual((status, r["path"]), (200, str(Path.home().resolve())))
        (self.dir / "f.txt").write_text("x")
        self.assertEqual(self.request("GET", "/api/dirs?path=" + quote(str(self.dir / "f.txt")))[0], 400)
        self.assertEqual(self.request("GET", "/api/dirs?path=" + quote(str(self.dir / "nope")))[0], 400)
        self.assertEqual(self.request("GET", "/api/dirs", headers={"Host": "evil.example"})[0], 403)


class ComposedServerTests(TmpDir):
    """Other tools (harbor) mount more APIs next to the convert one; routing must keep them apart."""

    class EchoAPI:
        def __init__(self):
            self.calls = []

        def state(self):
            return {"echo": len(self.calls)}

        def post(self, action, body):
            if action == "boom":
                raise ValueError("bad input")
            self.calls.append((action, body))
            return 200, {"action": action}

        def get(self, action, query):
            if action == "img":
                return (
                    200,
                    {"Content-Type": "image/webp", "Cache-Control": "private, max-age=60"},
                    b"RIFFxxxx",
                )
            if action == "boom":
                raise ValueError("no such thing")
            return (
                200,
                {"Content-Type": "application/json"},
                json.dumps({"action": action, "q": query}).encode(),
            )

    def setUp(self):
        super().setUp()
        self.queue = JobQueue(workers=1, image_threads=1).start()
        self.echo = self.EchoAPI()
        apis = {"/api/": ConvertAPI(self.queue, {"quality": 80}), "/api/extra/": self.echo}
        self.server = serve(
            0,
            UI_HTML,
            apis,
            allowed_origins={"chrome-extension://abcdefghijklmnop"},
            pages={"/other": b"<title>Other</title>"},
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.queue.shutdown()
        super().tearDown()

    def call(self, method, path, body=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if origin:
            headers["Origin"] = origin
        conn.request(
            method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_state_is_merged(self):
        status, st = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(st["echo"], 0)
        self.assertEqual(st["jobs"], [])

    def test_longest_prefix_wins(self):
        self.assertEqual(self.call("POST", "/api/extra/clear", {}), (200, {"action": "clear"}))
        self.assertEqual(self.echo.calls, [("clear", {})])
        self.assertEqual(self.call("POST", "/api/clear", {}), (200, {"cleared": 0}))
        self.assertEqual(len(self.echo.calls), 1)

    def raw_get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body, headers = resp.read(), dict(resp.getheaders())
        conn.close()
        return resp.status, headers, body

    def test_get_routed_to_api(self):
        status, headers, body = self.raw_get("/api/extra/list?page=2&page=3&q=x")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"action": "list", "q": {"page": "3", "q": "x"}})
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, headers, body = self.raw_get("/api/extra/img")
        self.assertEqual((status, headers["Content-Type"], body), (200, "image/webp", b"RIFFxxxx"))
        self.assertEqual(headers["Cache-Control"], "private, max-age=60")
        self.assertEqual(self.raw_get("/api/extra/boom")[0], 400)
        self.assertEqual(self.raw_get("/api/extra/a/b")[0], 404)
        self.assertEqual(self.raw_get("/api/add")[0], 404)  # ConvertAPI has no get()

    def test_extra_pages_and_framing(self):
        status, headers, body = self.raw_get("/other")
        self.assertEqual((status, body), (200, b"<title>Other</title>"))
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(self.raw_get("/")[1]["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(self.raw_get("/nope")[0], 404)

    def test_allowed_origins(self):
        ok = self.call("POST", "/api/extra/x", {}, origin="chrome-extension://abcdefghijklmnop")
        self.assertEqual(ok[0], 200)
        own = self.call("POST", "/api/extra/x", {}, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(own[0], 200)
        for bad in (
            "chrome-extension://ponmlkjihgfedcba",  # another extension
            "https://evil.example",
            f"https://127.0.0.1:{self.port}",  # right host, wrong scheme
            "null",
        ):
            with self.subTest(origin=bad):
                self.assertEqual(self.call("POST", "/api/extra/x", {}, origin=bad)[0], 403)
        self.assertEqual(len(self.echo.calls), 2)

    def test_api_errors_and_unknown_paths(self):
        self.assertEqual(self.call("POST", "/api/extra/boom", {}), (400, {"error": "bad input"}))
        self.assertEqual(self.call("POST", "/api/extra/a/b", {})[0], 404)
        self.assertEqual(self.call("POST", "/nope", {})[0], 404)
        self.assertEqual(self.call("POST", "/api/clear", [1, 2])[0], 400)


class AuthServerTests(TmpDir):
    """With ``auth``, the server asks it who each request is from and refuses the signed-out."""

    class FakeAuth:
        login_path = "/login"

        def public(self, method, path):
            return (method, path) in {("GET", "/login"), ("POST", "/api/me/login")}

        def user(self, headers):
            return {"t1": "ann", "t2": "bo"}.get(headers.get("X-Token", ""))

        def allows(self, user, method, path):
            return not (user == "bo" and path == "/api/dirs")  # bo may not browse folders

    class MeAPI:
        def __init__(self):
            self.seen = []

        def state(self):
            return {"me": current_user()}

        def post(self, action, body):
            self.seen.append(current_user())
            if action == "login":
                return 200, {"ok": True}, {"Set-Cookie": "s=1; HttpOnly"}
            return 200, {"user": current_user()}

        def get(self, action, query):
            return 200, {"Content-Type": "application/json"}, json.dumps({"user": current_user()}).encode()

    def setUp(self):
        super().setUp()
        self.me = self.MeAPI()
        self.server = serve(
            0, b"<title>Main</title>", {"/api/me/": self.me}, pages={"/login": b"login"}, auth=self.FakeAuth()
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def req(self, method, path, token=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"X-Token": token} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(
            method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers
        )
        resp = conn.getresponse()
        data, h = resp.read(), dict(resp.getheaders())
        conn.close()
        return resp.status, h, data

    def test_signed_out_requests_are_refused(self):
        status, h, _ = self.req("GET", "/")
        self.assertEqual((status, h["Location"]), (303, "/login"))
        for method, path, body in (
            ("GET", "/api/state", None),
            ("GET", "/api/dirs", None),
            ("GET", "/api/me/x", None),
            ("POST", "/api/me/x", {}),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.req(method, path, body=body)[0], 401)
        self.assertEqual(self.me.seen, [])
        self.assertEqual(self.req("GET", "/login")[2], b"login")  # public

    def test_public_post_can_set_headers(self):
        status, h, data = self.req("POST", "/api/me/login", body={})
        self.assertEqual((status, h["Set-Cookie"], json.loads(data)), (200, "s=1; HttpOnly", {"ok": True}))
        self.assertEqual(self.me.seen, [None])  # public, signed out

    def test_each_request_sees_its_own_user(self):
        self.assertEqual(self.req("GET", "/", token="t1")[0], 200)
        self.assertEqual(json.loads(self.req("GET", "/api/state", token="t1")[2]), {"me": "ann"})
        self.assertEqual(json.loads(self.req("GET", "/api/me/x", token="t2")[2]), {"user": "bo"})
        self.assertEqual(json.loads(self.req("POST", "/api/me/x", token="t2", body={})[2]), {"user": "bo"})
        self.assertIsNone(current_user())  # nothing leaks outside a request
        self.assertEqual(self.req("GET", "/api/state", token="nope")[0], 401)

    def test_allows_can_refuse_a_signed_in_user(self):
        self.assertEqual(self.req("GET", "/api/dirs", token="t1")[0], 200)
        status, _, data = self.req("GET", "/api/dirs", token="t2")
        self.assertEqual((status, json.loads(data)["error"]), (403, "you don't have permission for this"))
        self.assertEqual(self.req("GET", "/api/state", token="t2")[0], 200)

    def test_without_auth_there_is_no_user(self):
        self.assertIsNone(current_user())


if __name__ == "__main__":
    unittest.main()
