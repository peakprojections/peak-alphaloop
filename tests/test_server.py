import io
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from peak_video_editor.server import Workspace, make_handler

from test_media import HAS_FFMPEG, make_clip


@unittest.skipUnless(HAS_FFMPEG, "FFmpeg is not installed")
class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.workspace = Workspace(self.folder / "workspace")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.workspace))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, path, data=None, method=None, headers=None):
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers=headers or {})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.headers, response.read()

    def test_upload_preview_export_download(self) -> None:
        clip = make_clip(self.folder / "my clip.mp4")
        _, _, body = self.request("/api/upload", clip.read_bytes(), "PUT",
                                  {"X-Filename": "my%20clip.mp4"})
        info = json.loads(body)
        self.assertEqual(info["name"], "my clip.mp4")
        self.assertEqual(info["frame_count"], 12)

        for view in ("result", "matte", "original"):
            status, headers, png = self.request(
                f"/api/clips/{info['id']}/preview?t=0.1&view={view}&background=black"
                "&threshold=0.08&softness=0.1&unblend=true&protect_interior=true")
            self.assertEqual(headers["Content-Type"], "image/png")
            image = Image.open(io.BytesIO(png))
            self.assertEqual(image.size, (160, 96))
            self.assertEqual(image.mode, "RGBA" if view == "result" else "RGB")

        _, _, body = self.request(f"/api/clips/{info['id']}/sample?t=0.1&x=0.02&y=0.05")
        self.assertEqual(json.loads(body)["color"], "#000000")
        _, _, body = self.request(f"/api/clips/{info['id']}/sample?t=0.1&x=0.40&y=0.50")
        red, green, blue = (int(json.loads(body)["color"][i:i + 2], 16) for i in (1, 3, 5))
        self.assertGreater(red, 200)
        self.assertLess(blue, 60)

        _, _, body = self.request(
            f"/api/clips/{info['id']}/export",
            json.dumps({"format": "prores", "mirror_loop": True,
                        "settings": {"background": "black"}}).encode(),
            "POST", {"Content-Type": "application/json"})
        job = json.loads(body)
        deadline = time.monotonic() + 60
        while job["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.2)
            job = json.loads(self.request(f"/api/jobs/{job['id']}")[2])
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["filename"], "my clip-alpha-loop.mov")
        self.assertEqual(job["total"], 22)
        self.assertTrue(job["mirror_loop"])
        status, headers, data = self.request(job["download"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(len(data), job["size"])

    def test_unknown_ids_and_bad_upload(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/clips/" + "0" * 32 + "/preview")
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/upload", b"not a video", "PUT", {"X-Filename": "x.mp4"})
        self.assertEqual(caught.exception.code, 422)

    def test_index_page(self) -> None:
        status, headers, body = self.request("/")
        self.assertIn(b"Peak Video Editor", body)


if __name__ == "__main__":
    unittest.main()
