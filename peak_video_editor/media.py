"""FFmpeg-backed probing, frame decoding and alpha-preserving export."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator, Literal

import numpy as np

from .keying import KeySettings, key_frame

ExportFormat = Literal["webm", "prores"]

FORMATS: dict[str, dict[str, str]] = {
    "webm": {"suffix": ".webm", "label": "WebM (VP9 + alpha)"},
    "prores": {"suffix": ".mov", "label": "ProRes 4444 (.mov)"},
}

# Lower is better quality for VP9 CRF. 30 is visually clean for most overlays.
DEFAULT_WEBM_CRF = 30


class MediaError(RuntimeError):
    pass


class ExportCancelled(MediaError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int
    height: int
    frame_rate: Fraction
    duration: float
    frame_count: int
    codec: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.path.name,
            "width": self.width,
            "height": self.height,
            "frame_rate": float(self.frame_rate),
            "duration": self.duration,
            "frame_count": self.frame_count,
            "codec": self.codec,
        }


def find_binary(name: str) -> str:
    override = os.environ.get(f"PEAK_{name.upper()}")
    if override:
        return override
    found = shutil.which(name)
    if not found:
        raise MediaError(
            f"{name} was not found. Install FFmpeg and make sure {name} is on your PATH "
            f"(or set PEAK_{name.upper()} to its full path)."
        )
    return found


def probe(path: Path | str) -> VideoInfo:
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"File not found: {path}")
    completed = _run([
        find_binary("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames,duration"
        ":format=duration",
        "-of", "json", str(path),
    ], "Reading video information")
    try:
        payload = json.loads(completed.stdout)
        stream = payload["streams"][0]
    except (json.JSONDecodeError, KeyError, IndexError) as error:
        raise MediaError(f"{path.name} does not contain a readable video stream.") from error

    frame_rate = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate"))
    if not frame_rate:
        frame_rate = Fraction(30)
    duration = _float(stream.get("duration")) or _float(payload.get("format", {}).get("duration")) or 0.0
    frame_count = _int(stream.get("nb_frames")) or max(1, round(duration * float(frame_rate)))
    return VideoInfo(path, int(stream["width"]), int(stream["height"]), frame_rate,
                     duration, frame_count, str(stream.get("codec_name", "unknown")))


def read_frame(path: Path | str, time_seconds: float, max_width: int | None = None) -> np.ndarray:
    """Decode the frame at ``time_seconds`` as HxWx3 uint8, optionally downscaled."""
    info = probe(path)
    time_seconds = max(0.0, min(time_seconds, max(0.0, info.duration - 1.0 / float(info.frame_rate))))
    width, height = info.width, info.height
    filters = []
    if max_width and width > max_width:
        height = max(2, round(height * max_width / width / 2) * 2)
        width = max_width
        filters = ["-vf", f"scale={width}:{height}:flags=area"]
    command = [find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error",
               "-ss", f"{time_seconds:.4f}", "-i", str(info.path), "-map", "0:v:0",
               "-frames:v", "1", *filters, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    completed = subprocess.run(command, capture_output=True, check=False)
    expected = width * height * 3
    if completed.returncode != 0 or len(completed.stdout) < expected:
        details = completed.stderr.decode(errors="replace").strip()[-2000:]
        raise MediaError(f"Could not decode a frame at {time_seconds:.2f}s. {details}".strip())
    return np.frombuffer(completed.stdout[:expected], dtype=np.uint8).reshape(height, width, 3)


def iter_frames(info: VideoInfo) -> Iterator[np.ndarray]:
    command = [find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error",
               "-i", str(info.path), "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    frame_bytes = info.width * info.height * 3
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log)
        finished = False
        try:
            assert process.stdout is not None
            while True:
                raw = _read_exact(process.stdout, frame_bytes)
                if raw is None:
                    finished = True
                    break
                yield np.frombuffer(raw, dtype=np.uint8).reshape(info.height, info.width, 3)
        finally:
            process.stdout.close()  # type: ignore[union-attr]
            if not finished and process.poll() is None:
                process.kill()
            returncode = process.wait()
        if finished and returncode != 0:
            log.seek(0)
            details = log.read().decode(errors="replace").strip()[-2000:]
            raise MediaError(f"Decoding {info.path.name} failed: {details}")


def encoder_arguments(export_format: ExportFormat, webm_crf: int = DEFAULT_WEBM_CRF) -> list[str]:
    color_tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    if export_format == "webm":
        return ["-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuva420p",
                "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-crf", str(int(webm_crf)), "-b:v", "0",
                "-auto-alt-ref", "0", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4",
                *color_tags]
    if export_format == "prores":
        return ["-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuva444p10le",
                "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
                "-alpha_bits", "16", "-vendor", "apl0", *color_tags]
    raise MediaError(f"Unknown export format {export_format!r}; use 'webm' or 'prores'.")


def default_output_path(source: Path | str, export_format: ExportFormat) -> Path:
    source = Path(source)
    return source.with_name(f"{source.stem}-alpha{FORMATS[export_format]['suffix']}")


ProgressCallback = Callable[[int, int], None]


def export(source: Path | str, destination: Path | str, settings: KeySettings,
           export_format: ExportFormat, *, webm_crf: int = DEFAULT_WEBM_CRF,
           progress: ProgressCallback | None = None,
           cancel: threading.Event | None = None) -> Path:
    """Key every frame of ``source`` and write it with alpha to ``destination``."""
    info = probe(source)
    destination = Path(destination)
    expected_suffix = FORMATS[export_format]["suffix"]
    if destination.suffix.lower() != expected_suffix:
        raise MediaError(f"{FORMATS[export_format]['label']} output must end in {expected_suffix}.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.stem}.partial{expected_suffix}")
    settings = settings.validated()

    command = [find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{info.width}x{info.height}",
               "-framerate", str(info.frame_rate), "-i", "-",
               "-an", *encoder_arguments(export_format, webm_crf), str(partial)]
    written = 0
    with tempfile.TemporaryFile() as log:
        encoder = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log)
        try:
            assert encoder.stdin is not None
            for keyed in _keyed_frames(info, settings):
                if cancel is not None and cancel.is_set():
                    raise ExportCancelled("Export cancelled.")
                encoder.stdin.write(keyed.tobytes())
                written += 1
                if progress is not None:
                    progress(written, max(written, info.frame_count))
            encoder.stdin.close()
            returncode = encoder.wait()
        except BrokenPipeError:
            returncode = encoder.wait()
        except BaseException:
            encoder.kill()
            encoder.wait()
            partial.unlink(missing_ok=True)
            raise
        if returncode != 0 or written == 0:
            log.seek(0)
            details = log.read().decode(errors="replace").strip()[-2000:]
            partial.unlink(missing_ok=True)
            raise MediaError(f"Encoding failed after {written} frames: {details or 'no frames decoded'}")
    os.replace(partial, destination)
    if progress is not None:
        progress(written, written)
    return destination


def _keyed_frames(info: VideoInfo, settings: KeySettings) -> Iterator[np.ndarray]:
    """Key frames on a small thread pool while preserving frame order."""
    workers = max(1, min(6, (os.cpu_count() or 2) - 1))
    if workers == 1:
        for frame in iter_frames(info):
            yield key_frame(frame, settings)
        return
    pending: deque[Future[np.ndarray]] = deque()
    frames = iter_frames(info)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="key") as pool:
        try:
            for frame in frames:
                pending.append(pool.submit(key_frame, frame, settings))
                if len(pending) >= workers * 2:
                    yield pending.popleft().result()
            while pending:
                yield pending.popleft().result()
        finally:
            for future in pending:
                future.cancel()
            frames.close()


def read_alpha_range(path: Path | str) -> tuple[int, int]:
    """Decode the first frame the way peak-viewer will and return (min, max) alpha.

    WebM must be decoded with libvpx-vp9; FFmpeg's native VP9 decoder drops alpha.
    """
    path = Path(path)
    decoder = ["-c:v", "libvpx-vp9"] if path.suffix.lower() == ".webm" else []
    completed = subprocess.run(
        [find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", *decoder, "-i", str(path),
         "-frames:v", "1", "-vf", "format=rgba,alphaextract", "-f", "rawvideo", "-"],
        capture_output=True, check=False,
    )
    if completed.returncode != 0 or not completed.stdout:
        raise MediaError(f"Could not read alpha from {path.name}.")
    data = np.frombuffer(completed.stdout, dtype=np.uint8)
    return int(data.min()), int(data.max())


def _read_exact(stream, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _run(command: list[str], operation: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise MediaError(f"{operation} could not start: {error}") from error
    if completed.returncode != 0:
        details = completed.stderr.strip() or "no diagnostic output"
        raise MediaError(f"{operation} failed: {details[-2000:]}")
    return completed


def _parse_rate(value: object) -> Fraction | None:
    try:
        rate = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def _float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
