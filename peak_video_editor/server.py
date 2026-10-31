"""Local web app: preview and export alpha videos from a browser window.

Run ``peak-video-editor`` and it opens http://127.0.0.1:<port> in the default
browser. Everything runs on this machine; nothing is uploaded anywhere else.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import tempfile
import threading
import time
import uuid
import webbrowser
from collections import OrderedDict
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import numpy as np
from PIL import Image

from . import __version__
from .keying import PRESETS, KeySettings, alpha_matte, key_frame
from .media import (DEFAULT_WEBM_CRF, FORMATS, ExportCancelled, MediaError, VideoInfo,
                    export, probe, read_frame)

PREVIEW_MAX_WIDTH = 960
UPLOAD_CHUNK = 1024 * 1024
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class Clip:
    id: str
    path: Path
    info: VideoInfo
    display_name: str


@dataclass
class Job:
    id: str
    clip: Clip
    export_format: str
    settings: KeySettings
    output: Path
    status: str = "running"  # running | done | error | cancelled
    done: int = 0
    total: int = 0
    error: str | None = None
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict[str, Any]:
        elapsed = (self.finished or time.monotonic()) - self.started
        data: dict[str, Any] = {
            "id": self.id, "status": self.status, "done": self.done, "total": self.total,
            "error": self.error, "format": self.export_format, "elapsed": round(elapsed, 1),
            "filename": self.output.name,
        }
        if self.status == "done" and self.output.is_file():
            data["size"] = self.output.stat().st_size
            data["download"] = f"/api/jobs/{self.id}/download"
            data["stream"] = f"/api/jobs/{self.id}/file"
        return data


class Workspace:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(tempfile.mkdtemp(prefix="peak-video-editor-"))
        (self.root / "uploads").mkdir(parents=True, exist_ok=True)
        (self.root / "exports").mkdir(parents=True, exist_ok=True)
        self.clips: dict[str, Clip] = {}
        self.jobs: dict[str, Job] = {}
        self.frames: OrderedDict[tuple[str, float], np.ndarray] = OrderedDict()
        self.lock = threading.Lock()

    def add_clip(self, path: Path, display_name: str | None = None) -> Clip:
        info = probe(path)
        clip = Clip(uuid.uuid4().hex, path, info, display_name or path.name)
        with self.lock:
            self.clips[clip.id] = clip
        return clip

    def clip(self, clip_id: str) -> Clip:
        with self.lock:
            clip = self.clips.get(clip_id)
        if clip is None:
            raise LookupError("Unknown clip.")
        return clip

    def frame(self, clip: Clip, seconds: float) -> np.ndarray:
        key = (clip.id, round(seconds, 3))
        with self.lock:
            cached = self.frames.get(key)
            if cached is not None:
                self.frames.move_to_end(key)
                return cached
        frame = read_frame(clip.path, seconds, PREVIEW_MAX_WIDTH)
        with self.lock:
            self.frames[key] = frame
            while len(self.frames) > 12:
                self.frames.popitem(last=False)
        return frame

    def start_job(self, clip: Clip, export_format: str, settings: KeySettings, crf: int) -> Job:
        if export_format not in FORMATS:
            raise ValueError("Unknown export format.")
        job_id = uuid.uuid4().hex
        stem = Path(clip.display_name).stem or "video"
        folder = self.root / "exports" / job_id
        folder.mkdir()
        output = folder / f"{stem}-alpha{FORMATS[export_format]['suffix']}"
        job = Job(job_id, clip, export_format, settings, output)
        with self.lock:
            self.jobs[job_id] = job

        def progress(done: int, total: int) -> None:
            job.done, job.total = done, total

        def run() -> None:
            try:
                export(clip.path, output, settings, export_format,  # type: ignore[arg-type]
                       webm_crf=crf, progress=progress, cancel=job.cancel)
                job.status = "done"
            except ExportCancelled:
                job.status = "cancelled"
            except Exception as error:  # surfaced to the UI
                job.status, job.error = "error", str(error)
            finally:
                job.finished = time.monotonic()

        job.total = clip.info.frame_count
        threading.Thread(target=run, name=f"export-{job_id[:8]}", daemon=True).start()
        return job

    def job(self, job_id: str) -> Job:
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise LookupError("Unknown export.")
        return job

    def close(self) -> None:
        for job in list(self.jobs.values()):
            job.cancel.set()
        shutil.rmtree(self.root, ignore_errors=True)


def render_preview(frame: np.ndarray, settings: KeySettings, view: str) -> bytes:
    if view == "original":
        image = Image.fromarray(frame, "RGB")
    else:
        keyed = key_frame(frame, settings)
        image = (Image.fromarray(alpha_matte(keyed), "RGB") if view == "matte"
                 else Image.fromarray(keyed, "RGBA"))
    buffer = io.BytesIO()
    image.save(buffer, "PNG", compress_level=1)
    return buffer.getvalue()


def make_handler(workspace: Workspace):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"PeakVideoEditor/{__version__}"

        def log_message(self, format: str, *args: Any) -> None:  # quiet console
            pass

        # ---- routing -------------------------------------------------------------
        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            parts = [part for part in url.path.split("/") if part]
            query = {key: values[-1] for key, values in parse_qs(url.query).items()}
            try:
                if method == "GET" and not parts:
                    return self._static("index.html", "text/html; charset=utf-8")
                if method == "GET" and parts == ["api", "config"]:
                    return self._json({
                        "version": __version__,
                        "presets": {name: s.to_dict() for name, s in PRESETS.items()},
                        "formats": FORMATS, "default_crf": DEFAULT_WEBM_CRF,
                        "clips": [self._clip_dict(c) for c in workspace.clips.values()],
                    })
                if method == "PUT" and parts == ["api", "upload"]:
                    return self._upload()
                if len(parts) >= 3 and parts[:2] == ["api", "clips"]:
                    clip = workspace.clip(self._id(parts[2]))
                    if method == "GET" and parts[3:] == ["preview"]:
                        return self._preview(clip, query)
                    if method == "POST" and parts[3:] == ["export"]:
                        return self._export(clip)
                if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
                    job = workspace.job(self._id(parts[2]))
                    if method == "GET" and len(parts) == 3:
                        return self._json(job.to_dict())
                    if method == "POST" and parts[3:] == ["cancel"]:
                        job.cancel.set()
                        return self._json(job.to_dict())
                    if method == "GET" and parts[3:] in (["download"], ["file"]):
                        return self._send_file(job, attachment=parts[3] == "download")
                self._error(HTTPStatus.NOT_FOUND, "Not found.")
            except LookupError as error:
                self._error(HTTPStatus.NOT_FOUND, str(error))
            except (ValueError, KeyError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            except MediaError as error:
                self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(error))
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ---- endpoints -----------------------------------------------------------
        def _upload(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("Empty upload.")
            name = unquote(self.headers.get("X-Filename", "video")).replace("\\", "/").split("/")[-1]
            name = re.sub(r"[^\w.\- ()]+", "_", name).strip() or "video"
            folder = workspace.root / "uploads" / uuid.uuid4().hex
            folder.mkdir()
            destination = folder / name
            remaining = length
            with destination.open("wb") as handle:
                while remaining:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK, remaining))
                    if not chunk:
                        raise ValueError("Upload ended early.")
                    handle.write(chunk)
                    remaining -= len(chunk)
            try:
                clip = workspace.add_clip(destination, name)
            except MediaError:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            self._json(self._clip_dict(clip))

        def _preview(self, clip: Clip, query: dict[str, str]) -> None:
            settings = KeySettings.from_dict(query)
            seconds = float(query.get("t", "0"))
            view = query.get("view", "result")
            if view not in {"result", "matte", "original"}:
                raise ValueError("Unknown view.")
            body = render_preview(workspace.frame(clip, seconds), settings, view)
            self._send(HTTPStatus.OK, body, "image/png", {"Cache-Control": "no-store"})

        def _export(self, clip: Clip) -> None:
            payload = self._read_json()
            settings = KeySettings.from_dict(payload.get("settings", {}))
            crf = int(payload.get("crf", DEFAULT_WEBM_CRF))
            if not 4 <= crf <= 63:
                raise ValueError("WebM quality must be between 4 and 63.")
            job = workspace.start_job(clip, str(payload.get("format", "webm")), settings, crf)
            self._json(job.to_dict())

        def _send_file(self, job: Job, attachment: bool) -> None:
            if job.status != "done" or not job.output.is_file():
                raise LookupError("Export is not finished.")
            content_type = "video/webm" if job.output.suffix == ".webm" else "video/quicktime"
            size = job.output.stat().st_size
            start, end = 0, size - 1
            status = HTTPStatus.OK
            range_header = self.headers.get("Range")
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header or "")
            if match and not attachment and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else size - 1
                else:
                    start = max(0, size - int(match.group(2)))
                end = min(end, size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if attachment:
                self.send_header("Content-Disposition",
                                 f"attachment; filename*=UTF-8''{quote(job.output.name)}")
            self.end_headers()
            with job.output.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = handle.read(min(UPLOAD_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        # ---- helpers -------------------------------------------------------------
        @staticmethod
        def _id(value: str) -> str:
            if not ID_PATTERN.match(value):
                raise LookupError("Unknown id.")
            return value

        @staticmethod
        def _clip_dict(clip: Clip) -> dict[str, Any]:
            return {"id": clip.id, **clip.info.to_dict(), "name": clip.display_name}

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            return data

        def _static(self, name: str, content_type: str) -> None:
            body = resources.files("peak_video_editor").joinpath("static", name).read_bytes()
            self._send(HTTPStatus.OK, body, content_type, {"Cache-Control": "no-store"})

        def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._json({"error": message}, status)

        def _send(self, status: HTTPStatus, body: bytes, content_type: str,
                  headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(port: int = 8765, open_browser: bool = True, preload: list[Path] | None = None) -> None:
    workspace = Workspace()
    for path in preload or []:
        workspace.add_clip(path.resolve())
    server = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), make_handler(workspace))
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"No free port between {port} and {port + 19}.")
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Peak Video Editor {__version__} running at {url}  (Ctrl+C to quit)")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
        workspace.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="peak-video-editor",
                                     description="Open the Peak Projections alpha keyer in your browser.")
    parser.add_argument("videos", type=Path, nargs="*", help="Videos to open right away.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    try:
        serve(args.port, not args.no_browser, args.videos)
    except MediaError as error:
        print(f"peak-video-editor: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
