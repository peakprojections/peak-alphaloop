"""FFmpeg-backed probing, frame decoding and alpha-preserving export."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import zlib
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
# Fast zlib level for the mirror-loop spool; keyed frames compress well anyway.
SPOOL_COMPRESSION = 1


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
           cancel: threading.Event | None = None,
           mirror_loop: bool = False) -> Path:
    """Key every frame of ``source`` and write it with alpha to ``destination``.

    With ``mirror_loop`` the clip plays forward and then backward
    (frames 0..N-1 followed by N-2..1), so the last frame flows straight back
    into the first and the result loops seamlessly. The turnaround frames are
    not repeated, which would show up as a brief hold.
    """
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
    expected = mirrored_frame_count(info.frame_count) if mirror_loop else info.frame_count
    with tempfile.TemporaryFile() as log, contextlib.ExitStack() as stack:
        spool = stack.enter_context(FrameSpool()) if mirror_loop else None
        encoder = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log)
        try:
            assert encoder.stdin is not None

            def emit(frame_bytes: bytes) -> None:
                nonlocal written, expected
                if cancel is not None and cancel.is_set():
                    raise ExportCancelled("Export cancelled.")
                encoder.stdin.write(frame_bytes)  # type: ignore[union-attr]
                written += 1
                if progress is not None:
                    progress(written, max(written, expected))

            for keyed, packed in _keyed_frames(info, settings, compress=mirror_loop):
                emit(keyed.tobytes())
                if spool is not None and packed is not None:
                    spool.append(packed)
            if spool is not None:
                # Forward pass told us the real frame count; correct the estimate.
                expected = mirrored_frame_count(len(spool))
                for index in range(len(spool) - 2, 0, -1):
                    emit(spool.read(index))
            encoder.stdin.close()
            returncode = encoder.wait()
        except BrokenPipeError:
            returncode = encoder.wait()
        except BaseException:
            encoder.kill()
            encoder.wait()
            with contextlib.suppress(OSError):
                encoder.stdin.close()  # type: ignore[union-attr]
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


def _keyed_frames(info: VideoInfo, settings: KeySettings,
                  compress: bool = False) -> Iterator[tuple[np.ndarray, bytes | None]]:
    """Key frames on a small thread pool while preserving frame order.

    With ``compress`` each keyed frame is also zlib-packed on the worker thread
    (zlib releases the GIL) for the mirror-loop spool.
    """
    def work(frame: np.ndarray) -> tuple[np.ndarray, bytes | None]:
        keyed = key_frame(frame, settings)
        return keyed, (zlib.compress(keyed.tobytes(), SPOOL_COMPRESSION) if compress else None)

    workers = max(1, min(6, (os.cpu_count() or 2) - 1))
    frames = iter_frames(info)
    if workers == 1:
        try:
            for frame in frames:
                yield work(frame)
        finally:
            frames.close()
        return
    pending: deque[Future[tuple[np.ndarray, bytes | None]]] = deque()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="key") as pool:
        try:
            for frame in frames:
                pending.append(pool.submit(work, frame))
                if len(pending) >= workers * 2:
                    yield pending.popleft().result()
            while pending:
                yield pending.popleft().result()
        finally:
            for future in pending:
                future.cancel()
            frames.close()


def mirrored_frame_count(frame_count: int) -> int:
    """Frames in a forward-then-backward loop without repeated turnaround frames."""
    return frame_count + max(0, frame_count - 2)


class FrameSpool:
    """Compressed keyed frames on disk, so long clips can be replayed in reverse
    without holding them all in memory."""

    def __init__(self) -> None:
        self._file = tempfile.TemporaryFile(prefix="peak-loop-")
        self._index: list[tuple[int, int]] = []
        self._end = 0

    def __enter__(self) -> "FrameSpool":
        return self

    def __exit__(self, *exc: object) -> None:
        self._file.close()

    def __len__(self) -> int:
        return len(self._index)

    def append(self, packed: bytes) -> None:
        self._file.seek(self._end)
        self._file.write(packed)
        self._index.append((self._end, len(packed)))
        self._end += len(packed)

    def read(self, index: int) -> bytes:
        offset, length = self._index[index]
        self._file.seek(offset)
        return zlib.decompress(self._file.read(length))


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
