import io
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from peak_alphaloop.server import Workspace, make_handler

from test_media import HAS_FFMPEG, make_clip

TOKEN = "test-session-token"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@unittest.skipUnless(HAS_FFMPEG, "FFmpeg is not installed")
class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.workspace = Workspace(self.folder / "workspace")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.workspace, TOKEN))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.cookie = f"alphaloop_{self.port}={TOKEN}"

    def request(self, path, data=None, method=None, headers=None, cookie=True):
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.headers, response.read()

    def upload(self, path: Path, name: str | None = None) -> dict:
        _, _, body = self.request("/api/upload", path.read_bytes(), "PUT",
                                  {"X-Filename": urllib.request.quote(name or path.name)})
        return json.loads(body)

    def wait(self, job: dict) -> dict:
        deadline = time.monotonic() + 90
        while job["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.2)
            job = json.loads(self.request(f"/api/jobs/{job['id']}")[2])
        return job

    def export(self, payload: dict) -> dict:
        _, _, body = self.request("/api/export", json.dumps(payload).encode(), "POST",
                                  {"Content-Type": "application/json"})
        return self.wait(json.loads(body))

    # ---- security -------------------------------------------------------------
    def test_session_token_cookie_and_host_checks(self) -> None:
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(f"{self.base}/?token={TOKEN}", timeout=10)
        self.assertEqual(caught.exception.code, 303)
        self.assertIn(f"alphaloop_{self.port}={TOKEN}", caught.exception.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", caught.exception.headers["Set-Cookie"])
        self.assertEqual(caught.exception.headers["Location"], "/")

        status, _, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Peak", body)
        for path, kwargs in (("/", {"cookie": False}), ("/api/config", {"cookie": False}),
                             ("/?token=wrong", {"cookie": False})):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path, **kwargs)
            self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/config", headers={"Host": f"attacker.example:{self.port}"})
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/config", headers={"Cookie": f"alphaloop_{self.port}=nope"},
                         cookie=False)
        self.assertEqual(caught.exception.code, 403)

    # ---- editing + export -----------------------------------------------------
    def test_upload_preview_sample_and_timeline_export(self) -> None:
        clip = make_clip(self.folder / "my clip.mp4")
        info = self.upload(clip)
        self.assertEqual(info["name"], "my clip.mp4")
        self.assertEqual(info["frame_count"], 12)
        self.assertFalse(info["has_audio"])

        query = ("t=0.1&background=black&threshold=0.08&softness=0.1&unblend=true"
                 f"&protect_interior=true&canvas={info['id']}")
        for view, mode in (("result", "RGBA"), ("matte", "RGB"), ("original", "RGB"), ("plain", "RGB")):
            _, headers, png = self.request(f"/api/clips/{info['id']}/preview?{query}&view={view}")
            self.assertEqual(headers["Content-Type"], "image/png")
            image = Image.open(io.BytesIO(png))
            self.assertEqual((image.size, image.mode), ((160, 96), mode))
        _, headers, jpeg = self.request(
            f"/api/clips/{info['id']}/preview?t=0&view=plain&w=80&format=jpeg")
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(jpeg)).size, (80, 48))

        _, _, body = self.request(f"/api/clips/{info['id']}/sample?t=0.1&x=0.02&y=0.05")
        self.assertEqual(json.loads(body)["color"], "#000000")
        _, _, body = self.request(f"/api/clips/{info['id']}/sample?t=0.1&x=0.40&y=0.50")
        red, _, blue = (int(json.loads(body)["color"][i:i + 2], 16) for i in (1, 3, 5))
        self.assertGreater(red, 200)
        self.assertLess(blue, 60)

        job = self.export({
            "format": "prores", "mirror_loop": True, "settings": {"background": "black"},
            "segments": [{"clip": info["id"], "start": 0, "end": 0.25},
                         {"clip": info["id"], "start": 0.25, "end": 0.5, "reverse": True}],
        })
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["filename"], "my clip-alpha-loop.mov")
        self.assertEqual(job["total"], 22)   # 12 frames forward + 10 back
        status, headers, data = self.request(job["download"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(len(data), job["size"])

    def test_export_without_background_removal(self) -> None:
        clip = make_clip(self.folder / "plain.mp4")
        info = self.upload(clip)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/export", json.dumps({
                "format": "mp4", "segments": [{"clip": info["id"]}]}).encode(), "POST",
                {"Content-Type": "application/json"})
        self.assertEqual(caught.exception.code, 400)
        job = self.export({"format": "mp4", "remove_background": False, "mirror_loop": True,
                           "include_audio": True, "segments": [{"clip": info["id"]}]})
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["filename"], "plain-loop.mp4")
        self.assertFalse(job["remove_background"])
        status, headers, _ = self.request(job["stream"], headers={"Range": "bytes=0-99"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Type"], "video/mp4")

    def test_bad_requests(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/clips/" + "0" * 32 + "/preview")
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/upload", b"not a video", "PUT", {"X-Filename": "x.mp4"})
        self.assertEqual(caught.exception.code, 422)
        for payload in ({"segments": []}, {"segments": [{"clip": "0" * 32}]}):
            with self.subTest(payload=payload), self.assertRaises(urllib.error.HTTPError) as caught:
                self.request("/api/export", json.dumps(payload).encode(), "POST",
                             {"Content-Type": "application/json"})
            self.assertIn(caught.exception.code, (400, 404))

    def test_config_ping_and_quit(self) -> None:
        config = json.loads(self.request("/api/config")[2])
        self.assertIn("color_presets", config)
        self.assertFalse(config["standalone"])
        self.request("/api/ping", b"", "POST")
        self.assertIsNotNone(self.workspace.last_seen)
        self.request("/api/quit", b"", "POST")
        self.assertTrue(self.workspace.stop.is_set())


if __name__ == "__main__":
    unittest.main()


class SingleInstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        import os
        from unittest import mock
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.folder),
                                               "LOCALAPPDATA": str(self.folder),
                                               "ALPHALOOP_NO_UPDATE_CHECK": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_launch_reuses_the_running_copy(self) -> None:
        from unittest import mock
        from peak_alphaloop import server as server_module
        opened: list[str] = []
        with mock.patch.object(server_module.webbrowser, "open", side_effect=opened.append), \
                mock.patch.object(server_module, "state_dir", return_value=self.folder / "state"):
            first = threading.Thread(target=server_module.serve,
                                     kwargs={"port": 18765, "open_browser": False}, daemon=True)
            first.start()
            deadline = time.monotonic() + 10
            while server_module.find_running_instance() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            running = server_module.find_running_instance()
            self.assertIsNotNone(running)
            self.assertEqual(running["version"], server_module.__version__)

            # A second launch must not start another server; it opens the first one.
            started = time.monotonic()
            server_module.serve(port=18765, open_browser=True)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(opened, [f"http://127.0.0.1:{running['port']}/?token={running['token']}"])

            server_module._instance_request(running, "/api/quit", "POST")
            first.join(timeout=15)
            self.assertFalse(first.is_alive())
            self.assertIsNone(server_module.find_running_instance())
            self.assertFalse((self.folder / "state" / "instance.json").exists())

    def test_stale_link_shows_a_friendly_page(self) -> None:
        workspace = Workspace(self.folder / "ws")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(workspace, TOKEN))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/?token=old-token", timeout=10)
        self.assertEqual(caught.exception.code, 403)
        self.assertIn("text/html", caught.exception.headers["Content-Type"])
        self.assertIn(b"expired", caught.exception.read())
