"""Local web app: edit, key and export videos from a browser window.

Run ``alphaloop`` and it opens the app in your default browser. Everything
runs on this machine; videos are never uploaded anywhere else.

The server only listens on 127.0.0.1 and only answers requests that carry the
per-session token it hands to the browser it opened (as a SameSite=Strict
cookie) and that address it by its own host name. Other web pages open in the
same browser therefore cannot drive it.
"""

from __future__ import annotations

import argparse
import hmac
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import urllib.request
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

from . import __version__, updater
from .keying import COLOR_PRESETS, PRESETS, KeySettings, alpha_matte, key_frame
from .media import (DEFAULT_WEBM_CRF, FORMATS, Canvas, ExportCancelled, MediaError, Segment,
                    VideoInfo, export_timeline, output_suffix, pad_color, probe, read_frame,
                    timeline_frame_count)

PREVIEW_MAX_WIDTH = 960
UPLOAD_CHUNK = 1024 * 1024
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
# A standalone app with no browser tab left open shuts itself down after this.
IDLE_SHUTDOWN_SECONDS = 150
# ...and one whose browser tab never connected at all gives up after this.
NEVER_CONNECTED_SECONDS = 600


@dataclass
class Clip:
    id: str
    path: Path
    info: VideoInfo
    display_name: str


@dataclass
class Job:
    id: str
    export_format: str
    output: Path
    mirror_loop: bool = False
    remove_background: bool = True
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
            "filename": self.output.name, "mirror_loop": self.mirror_loop,
            "remove_background": self.remove_background,
        }
        if self.status == "done" and self.output.is_file():
            data["size"] = self.output.stat().st_size
            data["download"] = f"/api/jobs/{self.id}/download"
            data["stream"] = f"/api/jobs/{self.id}/file"
        return data


class Workspace:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(tempfile.mkdtemp(prefix="peak-alphaloop-"))
        (self.root / "uploads").mkdir(parents=True, exist_ok=True)
        (self.root / "exports").mkdir(parents=True, exist_ok=True)
        self.clips: dict[str, Clip] = {}
        self.jobs: dict[str, Job] = {}
        self.frames: OrderedDict[tuple, np.ndarray] = OrderedDict()
        self.lock = threading.Lock()
        self.last_seen: float | None = None
        self.stop = threading.Event()

    # ---- clips ---------------------------------------------------------------
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

    def frame(self, clip: Clip, seconds: float, canvas: Canvas | None = None,
              pad_rgb: tuple[int, int, int] = (0, 0, 0),
              max_width: int = PREVIEW_MAX_WIDTH) -> np.ndarray:
        key = (clip.id, round(seconds, 3), canvas, pad_rgb, max_width)
        with self.lock:
            cached = self.frames.get(key)
            if cached is not None:
                self.frames.move_to_end(key)
                return cached
        frame = read_frame(clip.path, seconds, max_width, canvas, pad_rgb)
        with self.lock:
            self.frames[key] = frame
            while len(self.frames) > 48:
                self.frames.popitem(last=False)
        return frame

    # ---- exports -------------------------------------------------------------
    def start_job(self, segments: list[Segment], name: str, export_format: str,
                  settings: KeySettings, crf: int, mirror_loop: bool = False,
                  remove_background: bool = True, include_audio: bool = False,
                  canvas: Canvas | None = None) -> Job:
        if export_format not in FORMATS:
            raise ValueError("Unknown export format.")
        if remove_background and not FORMATS[export_format]["alpha"]:
            raise ValueError("MP4 cannot hold transparency; turn background removal off "
                             "or choose WebM or ProRes.")
        if not segments:
            raise ValueError("Add at least one clip to the timeline.")
        job_id = uuid.uuid4().hex
        stem = Path(name).stem or "video"
        folder = self.root / "exports" / job_id
        folder.mkdir()
        suffix = output_suffix(remove_background, mirror_loop)
        output = folder / f"{stem}{suffix}{FORMATS[export_format]['suffix']}"
        job = Job(job_id, export_format, output, mirror_loop, remove_background)
        canvas = canvas or Canvas.of(probe(segments[0].path))
        job.total = timeline_frame_count(segments, canvas, mirror_loop)
        with self.lock:
            self.jobs[job_id] = job

        def progress(done: int, total: int) -> None:
            job.done, job.total = done, total

        def run() -> None:
            try:
                export_timeline(segments, output, settings, export_format,  # type: ignore[arg-type]
                                webm_crf=crf, progress=progress, cancel=job.cancel,
                                mirror_loop=mirror_loop, remove_background=remove_background,
                                include_audio=include_audio, canvas=canvas)
                job.status = "done"
            except ExportCancelled:
                job.status = "cancelled"
            except Exception as error:  # surfaced to the UI
                job.status, job.error = "error", str(error)
            finally:
                job.finished = time.monotonic()

        threading.Thread(target=run, name=f"export-{job_id[:8]}", daemon=True).start()
        return job

    def job(self, job_id: str) -> Job:
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise LookupError("Unknown export.")
        return job

    def busy(self) -> bool:
        with self.lock:
            return any(job.status == "running" for job in self.jobs.values())

    def close(self) -> None:
        for job in list(self.jobs.values()):
            job.cancel.set()
        shutil.rmtree(self.root, ignore_errors=True)


def render_preview(frame: np.ndarray, settings: KeySettings, view: str,
                   image_format: str = "png") -> bytes:
    if view == "original" or view == "plain":
        image = Image.fromarray(frame, "RGB")
    else:
        keyed = key_frame(frame, settings)
        image = (Image.fromarray(alpha_matte(keyed), "RGB") if view == "matte"
                 else Image.fromarray(keyed, "RGBA"))
    buffer = io.BytesIO()
    if image_format == "jpeg":
        image.convert("RGB").save(buffer, "JPEG", quality=80)
    else:
        image.save(buffer, "PNG", compress_level=1)
    return buffer.getvalue()


def _canvas_from_query(workspace: Workspace, query: dict[str, str]) -> Canvas | None:
    """The preview canvas is the first timeline clip's geometry (``canvas=<clip id>``)."""
    canvas_id = query.get("canvas")
    if not canvas_id or not ID_PATTERN.match(canvas_id):
        return None
    return Canvas.of(workspace.clip(canvas_id).info)


def make_handler(workspace: Workspace, token: str | None = None):
    """``token=None`` disables the session check (used only by unit tests)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = f"PeakAlphaloop/{__version__}"

        def log_message(self, format: str, *args: Any) -> None:  # quiet console
            pass

        # ---- routing -------------------------------------------------------------
        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        @property
        def _cookie_name(self) -> str:
            return f"alphaloop_{self.server.server_address[1]}"

        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            host = (self.headers.get("Host") or "").lower()
            return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _session_ok(self) -> bool:
            if token is None:
                return True
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == self._cookie_name and hmac.compare_digest(value, token):
                    return True
            return False

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            parts = [part for part in url.path.split("/") if part]
            query = {key: values[-1] for key, values in parse_qs(url.query).items()}
            try:
                if token is not None and not self._host_ok():
                    return self._error(HTTPStatus.FORBIDDEN, "Unexpected host.")
                if method == "GET" and not parts:
                    return self._index(query)
                if method == "GET" and parts == ["favicon.png"]:
                    body = resources.files("peak_alphaloop").joinpath("static", "favicon.png").read_bytes()
                    return self._send(HTTPStatus.OK, body, "image/png",
                                      {"Cache-Control": "max-age=86400"})
                if not self._session_ok():
                    return self._error(HTTPStatus.FORBIDDEN,
                                       "Session expired. Reopen Peak Alphaloop from its launcher.")
                workspace.last_seen = time.monotonic()
                if method == "GET" and parts == ["api", "config"]:
                    return self._json({
                        "version": __version__,
                        "presets": {name: s.to_dict() for name, s in PRESETS.items()},
                        "color_presets": {name: s.to_dict() for name, s in COLOR_PRESETS.items()},
                        "formats": FORMATS, "default_crf": DEFAULT_WEBM_CRF,
                        "clips": [self._clip_dict(c) for c in workspace.clips.values()],
                        "standalone": updater.is_standalone(),
                    })
                if method == "POST" and parts == ["api", "ping"]:
                    return self._json({"ok": True})
                if method == "POST" and parts == ["api", "quit"]:
                    self._json({"ok": True})
                    workspace.stop.set()
                    return None
                if method == "GET" and parts == ["api", "update"]:
                    return self._json(updater.check_cached())
                if method == "POST" and parts == ["api", "update", "install"]:
                    message = updater.install_update()
                    self._json({"ok": True, "message": message})
                    workspace.stop.set()
                    return None
                if method == "PUT" and parts == ["api", "upload"]:
                    return self._upload()
                if method == "POST" and parts == ["api", "export"]:
                    return self._export()
                if len(parts) >= 3 and parts[:2] == ["api", "clips"]:
                    clip = workspace.clip(self._id(parts[2]))
                    if method == "GET" and parts[3:] == ["preview"]:
                        return self._preview(clip, query)
                    if method == "GET" and parts[3:] == ["sample"]:
                        return self._sample(clip, query)
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
            except (ValueError, KeyError, TypeError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            except (MediaError, updater.UpdateError) as error:
                self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(error))
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ---- endpoints -----------------------------------------------------------
        def _index(self, query: dict[str, str]) -> None:
            supplied = query.get("token")
            if token is not None and supplied is not None:
                if not hmac.compare_digest(supplied, token):
                    return self._stale_page()
                # Swap the one-time URL token for a cookie and drop it from the address bar.
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Set-Cookie", f"{self._cookie_name}={token}; Path=/; "
                                               "HttpOnly; SameSite=Strict")
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if not self._session_ok():
                return self._stale_page()
            body = resources.files("peak_alphaloop").joinpath("static", "index.html").read_bytes()
            self._send(HTTPStatus.OK, body, "text/html; charset=utf-8", {"Cache-Control": "no-store"})

        def _stale_page(self) -> None:
            body = ("<!DOCTYPE html><meta charset=utf-8><title>Peak Alphaloop</title>"
                    "<link rel=icon href=/favicon.png>"
                    "<body style='font-family:system-ui,sans-serif;background:#0d1016;color:#e8ecf3;"
                    "display:grid;place-items:center;height:90vh;margin:0'><div style='max-width:460px'>"
                    "<h2>This Peak Alphaloop link has expired</h2><p style='color:#a9b3c4;"
                    "line-height:1.5'>It belongs to a session that is no longer running. Close "
                    "this tab and open Peak Alphaloop again; it will open a fresh tab.</p>"
                    "</div></body>").encode()
            self._send(HTTPStatus.FORBIDDEN, body, "text/html; charset=utf-8")

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
            if view not in {"result", "matte", "original", "plain"}:
                raise ValueError("Unknown view.")
            remove_background = view != "plain"
            canvas = _canvas_from_query(workspace, query)
            width = max(32, min(PREVIEW_MAX_WIDTH, int(query.get("w", PREVIEW_MAX_WIDTH))))
            frame = workspace.frame(clip, seconds, canvas, pad_color(settings, remove_background),
                                    width)
            image_format = "jpeg" if query.get("format") == "jpeg" else "png"
            body = render_preview(frame, settings, view, image_format)
            self._send(HTTPStatus.OK, body, f"image/{image_format}",
                       {"Cache-Control": "private, max-age=600" if image_format == "jpeg"
                        else "no-store"})

        def _sample(self, clip: Clip, query: dict[str, str]) -> None:
            """Median color around a point; x and y are 0-1 fractions of the frame."""
            canvas = _canvas_from_query(workspace, query)
            frame = workspace.frame(clip, float(query.get("t", "0")), canvas)
            height, width = frame.shape[:2]
            x = min(width - 1, max(0, int(float(query["x"]) * width)))
            y = min(height - 1, max(0, int(float(query["y"]) * height)))
            radius = max(1, int(query.get("radius", "3")))
            patch = frame[max(0, y - radius):y + radius + 1, max(0, x - radius):x + radius + 1]
            red, green, blue = (int(v) for v in np.median(patch.reshape(-1, 3), axis=0))
            self._json({"color": f"#{red:02x}{green:02x}{blue:02x}"})

        def _export(self) -> None:
            payload = self._read_json()
            settings = KeySettings.from_dict(payload.get("settings", {}))
            crf = int(payload.get("crf", DEFAULT_WEBM_CRF))
            if not 4 <= crf <= 63:
                raise ValueError("WebM quality must be between 4 and 63.")
            raw_segments = payload.get("segments")
            if not isinstance(raw_segments, list) or not raw_segments:
                raise ValueError("Add at least one clip to the timeline.")
            if len(raw_segments) > 500:
                raise ValueError("Too many segments.")
            segments: list[Segment] = []
            clips: list[Clip] = []
            for item in raw_segments:
                clip = workspace.clip(self._id(str(item["clip"])))
                start = float(item.get("start", 0.0))
                end = float(item.get("end", clip.info.duration))
                if not 0 <= start < end:
                    raise ValueError("Each segment needs an end after its start.")
                clips.append(clip)
                segments.append(Segment(clip.path, start, end, bool(item.get("reverse", False))))
            job = workspace.start_job(
                segments, clips[0].display_name, str(payload.get("format", "webm")), settings, crf,
                mirror_loop=bool(payload.get("mirror_loop", False)),
                remove_background=bool(payload.get("remove_background", True)),
                include_audio=bool(payload.get("include_audio", False)),
                canvas=Canvas.of(clips[0].info))
            self._json(job.to_dict())

        def _send_file(self, job: Job, attachment: bool) -> None:
            if job.status != "done" or not job.output.is_file():
                raise LookupError("Export is not finished.")
            content_type = {".webm": "video/webm", ".mp4": "video/mp4"}.get(
                job.output.suffix, "video/quicktime")
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

        def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._json({"error": message}, status)

        def _send(self, status: HTTPStatus, body: bytes, content_type: str,
                  headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


class LocalServer(ThreadingHTTPServer):
    """HTTP server that owns its port exclusively.

    Python's default SO_REUSEADDR lets two processes listen on the same port on
    Windows, so a second copy of the app would silently share the first one's
    port and hand out links the first copy rejects.
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if os.name == "nt" and exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


def state_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Peak Alphaloop"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Peak Alphaloop"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "peak-alphaloop"


def instance_file() -> Path:
    return state_dir() / "instance.json"


def _instance_request(info: dict[str, Any], path: str, method: str = "GET") -> dict[str, Any] | None:
    port, session = int(info["port"]), str(info["token"])
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method, data=b"" if method == "POST" else None,
        headers={"Cookie": f"alphaloop_{port}={session}"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            return json.loads(response.read() or b"{}")
    except (OSError, ValueError):
        return None


def find_running_instance() -> dict[str, Any] | None:
    """The already-running copy of the app (if any) and whether it is this version."""
    try:
        info = json.loads(instance_file().read_text(encoding="utf-8"))
        info["port"], info["token"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    config = _instance_request(info, "/api/config")
    if config is None:
        return None
    info["version"] = config.get("version")
    return info


def remove_stale_workspaces(max_age_hours: float = 12) -> None:
    """Delete temp folders left by sessions that crashed or were killed."""
    cutoff = time.time() - max_age_hours * 3600
    for folder in Path(tempfile.gettempdir()).glob("peak-alphaloop-*"):
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


def serve(port: int = 8765, open_browser: bool = True, preload: list[Path] | None = None,
          exit_when_closed: bool | None = None, single_instance: bool = True) -> None:
    if single_instance:
        running = find_running_instance()
        if running is not None and running.get("version") == __version__:
            url = f"http://127.0.0.1:{running['port']}/?token={running['token']}"
            print(f"Peak Alphaloop is already running; opening it.\n  Open: {url}", flush=True)
            if open_browser:
                webbrowser.open(url)
            return
        if running is not None:
            # An older/newer copy (e.g. right after an update): ask it to close first.
            print(f"Closing the running Peak Alphaloop {running.get('version')}.", flush=True)
            _instance_request(running, "/api/quit", "POST")
            for _ in range(40):
                if find_running_instance() is None:
                    break
                time.sleep(0.25)
    remove_stale_workspaces()
    workspace = Workspace()
    for path in preload or []:
        workspace.add_clip(path.resolve())
    token = secrets.token_urlsafe(24)
    server = None
    for candidate in range(port, port + 20):
        try:
            server = LocalServer(("127.0.0.1", candidate), make_handler(workspace, token))
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"No free port between {port} and {port + 19}.")
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    record = instance_file() if single_instance else None
    if record is not None:
        try:
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps({"port": server.server_address[1], "token": token,
                                          "pid": os.getpid(), "version": __version__}),
                              encoding="utf-8")
        except OSError:
            record = None
    print(f"Peak Alphaloop {__version__} is running.\n  Open: {url}\n  (Ctrl+C or the Quit "
          "button to stop)", flush=True)
    if exit_when_closed is None:
        exit_when_closed = updater.is_standalone()
    updater.cleanup_previous_install()
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()

    started = time.monotonic()

    def watchdog() -> None:
        while not workspace.stop.wait(5):
            seen = workspace.last_seen
            idle_since = seen if seen is not None else started
            # Before any tab connects, allow longer (slow first launch, browser prompts).
            limit = IDLE_SHUTDOWN_SECONDS if seen is not None else NEVER_CONNECTED_SECONDS
            if (exit_when_closed and not workspace.busy()
                    and time.monotonic() - idle_since > limit):
                print("No browser window open; shutting down.", flush=True)
                workspace.stop.set()
        server.shutdown()

    threading.Thread(target=watchdog, name="watchdog", daemon=True).start()
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name) and threading.current_thread() is threading.main_thread():
            signal.signal(getattr(signal, name), lambda *_: workspace.stop.set())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
    finally:
        workspace.stop.set()
        server.server_close()
        workspace.close()
        if record is not None:
            try:
                if json.loads(record.read_text(encoding="utf-8")).get("token") == token:
                    record.unlink()
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alphaloop",
                                     description="Open Peak Alphaloop in your browser.")
    parser.add_argument("videos", type=Path, nargs="*", help="Videos to open right away.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window.")
    parser.add_argument("--new-instance", action="store_true",
                        help="Start a separate copy even if one is already running.")
    parser.add_argument("--no-update-check", action="store_true",
                        help="Do not check GitHub for a newer release.")
    parser.add_argument("--self-test", metavar="REPORT", type=Path, nargs="?", const=Path("-"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args, unknown = parser.parse_known_args(argv)
    # macOS Finder can pass a "-psn_0_12345" process serial number; ignore it.
    unknown = [item for item in unknown if not item.startswith("-psn_")]
    if unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    if args.self_test is not None:
        from . import selftest
        return selftest.run(None if str(args.self_test) == "-" else args.self_test)
    if args.no_update_check:
        os.environ["ALPHALOOP_NO_UPDATE_CHECK"] = "1"
    try:
        serve(args.port, not args.no_browser, args.videos,
              single_instance=not args.new_instance and not args.videos)
    except MediaError as error:
        print(f"alphaloop: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
