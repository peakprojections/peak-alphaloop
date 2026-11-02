"""FFmpeg-backed probing, timeline decoding and alpha-preserving export.

An export is a *timeline*: an ordered list of segments, each a slice of a
source clip that may play reversed. Every segment is decoded onto one common
canvas (size and frame rate of the first clip), optionally keyed to alpha,
optionally mirrored into a seamless loop, and encoded in one pass.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zlib
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable, Iterator, Literal, Sequence

import numpy as np

from .keying import KeySettings, key_frame

ExportFormat = Literal["webm", "prores", "mp4"]

# "alpha" says whether the format can carry transparency. MP4 (H.264) is only
# offered when background removal is off.
FORMATS: dict[str, dict[str, object]] = {
    "webm": {"suffix": ".webm", "label": "WebM (VP9 + alpha)", "plain_label": "WebM (VP9)",
             "alpha": True},
    "prores": {"suffix": ".mov", "label": "ProRes 4444 (.mov)", "plain_label": "ProRes 422 HQ (.mov)",
               "alpha": True},
    "mp4": {"suffix": ".mp4", "label": "MP4 (H.264)", "plain_label": "MP4 (H.264)", "alpha": False},
}
AUDIO_CODECS = {
    "webm": ["-c:a", "libopus", "-b:a", "160k"],
    "mp4": ["-c:a", "aac", "-b:a", "192k"],
    "prores": ["-c:a", "pcm_s16le"],
}
# Quality for H.264 output; 18 is visually lossless for most footage.
DEFAULT_MP4_CRF = 18
# Lower is better quality for VP9 CRF. 30 is visually clean for most overlays.
DEFAULT_WEBM_CRF = 30
# Fast zlib level for frame spools; keyed frames compress well anyway.
SPOOL_COMPRESSION = 1
AUDIO_RATE = 48000


# Windowed (no console) builds must not flash a console window per FFmpeg call,
# and must not hand FFmpeg an invalid stdin.
_SUBPROCESS = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
               if os.name == "nt" else {})


class MediaError(RuntimeError):
    pass


class ExportCancelled(MediaError):
    pass


# --------------------------------------------------------------------------- probing

@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int          # as displayed (rotation applied)
    height: int
    frame_rate: Fraction
    duration: float
    frame_count: int
    codec: str
    has_audio: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.path.name,
            "width": self.width,
            "height": self.height,
            "frame_rate": float(self.frame_rate),
            "duration": self.duration,
            "frame_count": self.frame_count,
            "codec": self.codec,
            "has_audio": self.has_audio,
        }


def _bundled_dirs() -> list[Path]:
    """Folders a standalone build may ship FFmpeg in."""
    dirs: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        dirs += [Path(frozen_root) / "ffmpeg", Path(frozen_root) / "ffmpeg" / "bin"]
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        dirs += [executable_dir / "ffmpeg", executable_dir.parent / "Resources" / "ffmpeg"]
    return dirs


def find_binary(name: str) -> str:
    override = os.environ.get(f"PEAK_{name.upper()}")
    if override:
        return override
    executable = name + (".exe" if os.name == "nt" else "")
    for folder in _bundled_dirs():
        candidate = folder / executable
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if not found:
        raise MediaError(
            f"{name} was not found. Install FFmpeg and make sure {name} is on your PATH "
            f"(or set PEAK_{name.upper()} to its full path)."
        )
    return found


_PROBE_CACHE: dict[tuple[str, float, int], VideoInfo] = {}
_PROBE_LOCK = threading.Lock()


def probe(path: Path | str) -> VideoInfo:
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"File not found: {path}")
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime, stat.st_size)
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached
    completed = _run([
        find_binary("ffprobe"), "-v", "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames,"
        "duration:stream_tags=rotate:stream_side_data=rotation:format=duration",
        "-of", "json", str(path),
    ], "Reading video information")
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        stream = next(s for s in streams if s.get("codec_type") == "video")
    except (json.JSONDecodeError, StopIteration) as error:
        raise MediaError(f"{path.name} does not contain a readable video stream.") from error

    frame_rate = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate"))
    if not frame_rate or frame_rate > 240:
        frame_rate = Fraction(30)
    duration = _float(stream.get("duration")) or _float(payload.get("format", {}).get("duration")) or 0.0
    frame_count = _int(stream.get("nb_frames")) or max(1, round(duration * float(frame_rate)))
    if not duration:
        duration = frame_count / float(frame_rate)
    width, height = int(stream["width"]), int(stream["height"])
    if _rotation(stream) % 180 == 90:  # phone footage: FFmpeg auto-rotates on decode
        width, height = height, width
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    info = VideoInfo(path, width, height, frame_rate, duration, frame_count,
                     str(stream.get("codec_name", "unknown")), has_audio)
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = info
    return info


def _rotation(stream: dict) -> int:
    for side_data in stream.get("side_data_list", []) or []:
        if "rotation" in side_data:
            with contextlib.suppress(TypeError, ValueError):
                return abs(int(float(side_data["rotation"])))
    with contextlib.suppress(TypeError, ValueError):
        return abs(int(stream.get("tags", {}).get("rotate", 0)))
    return 0


# --------------------------------------------------------------------------- timeline

@dataclass(frozen=True)
class Canvas:
    """Output geometry every segment is fitted onto."""
    width: int
    height: int
    frame_rate: Fraction

    @classmethod
    def of(cls, info: VideoInfo) -> "Canvas":
        return cls(info.width, info.height, info.frame_rate)

    @property
    def fps(self) -> float:
        return float(self.frame_rate)


@dataclass(frozen=True)
class Segment:
    """A slice [start, end) of a source clip, in source seconds."""
    path: Path
    start: float = 0.0
    end: float | None = None
    reverse: bool = False

    def resolved(self) -> "Segment":
        info = probe(self.path)
        end = info.duration if self.end is None else min(float(self.end), info.duration)
        start = max(0.0, min(float(self.start), end))
        return Segment(Path(self.path), start, end, bool(self.reverse))

    def frame_count(self, canvas: Canvas) -> int:
        end = self.end if self.end is not None else probe(self.path).duration
        return max(1, round((end - self.start) * canvas.fps))


def canvas_filter(canvas: Canvas, pad_rgb: tuple[int, int, int] = (0, 0, 0),
                  fps: bool = True) -> str:
    """Scale to fit the canvas, pad the rest with ``pad_rgb``, convert to RGB."""
    pad = "0x{:02x}{:02x}{:02x}".format(*pad_rgb)
    parts = [f"fps={canvas.frame_rate}"] if fps else []
    parts += [
        f"scale={canvas.width}:{canvas.height}:force_original_aspect_ratio=decrease:flags=bicubic",
        f"pad={canvas.width}:{canvas.height}:(ow-iw)/2:(oh-ih)/2:color={pad}",
        "setsar=1", "format=rgb24",
    ]
    return ",".join(parts)


def pad_color(settings: KeySettings, remove_background: bool) -> tuple[int, int, int]:
    """Letterbox bars use the keyed color so they turn transparent too."""
    if not remove_background:
        return (0, 0, 0)
    if settings.background == "white":
        return (255, 255, 255)
    if settings.background == "color":
        return settings.key_rgb
    return (0, 0, 0)


def read_frame(path: Path | str, time_seconds: float, max_width: int | None = None,
               canvas: Canvas | None = None,
               pad_rgb: tuple[int, int, int] = (0, 0, 0)) -> np.ndarray:
    """Decode the frame at ``time_seconds`` as HxWx3 uint8.

    With ``canvas`` the frame is fitted exactly as an export would place it.
    ``max_width`` downscales the result for previews.
    """
    info = probe(path)
    time_seconds = max(0.0, min(time_seconds, max(0.0, info.duration - 1.0 / float(info.frame_rate))))
    target = canvas or Canvas.of(info)
    width, height = target.width, target.height
    if max_width and width > max_width:
        height = max(2, round(height * max_width / width / 2) * 2)
        width = max_width
    fitted = Canvas(width, height, target.frame_rate)
    command = [find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error",
               "-ss", f"{time_seconds:.4f}", "-i", str(info.path), "-map", "0:v:0",
               "-frames:v", "1", "-vf", canvas_filter(fitted, pad_rgb, fps=False),
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    completed = subprocess.run(command, capture_output=True, check=False,
                               stdin=subprocess.DEVNULL, **_SUBPROCESS)
    expected = width * height * 3
    if completed.returncode != 0 or len(completed.stdout) < expected:
        details = completed.stderr.decode(errors="replace").strip()[-2000:]
        raise MediaError(f"Could not decode a frame at {time_seconds:.2f}s. {details}".strip())
    return np.frombuffer(completed.stdout[:expected], dtype=np.uint8).reshape(height, width, 3)


def iter_segment_frames(segment: Segment, canvas: Canvas,
                        pad_rgb: tuple[int, int, int] = (0, 0, 0)) -> Iterator[np.ndarray]:
    """Exactly ``segment.frame_count(canvas)`` RGB frames on the canvas."""
    segment = segment.resolved()
    count = segment.frame_count(canvas)
    if not segment.reverse:
        yield from _decode_segment(segment, canvas, pad_rgb, count)
        return
    with FrameSpool() as spool:
        for frame in _decode_segment(segment, canvas, pad_rgb, count):
            spool.append(zlib.compress(frame.tobytes(), SPOOL_COMPRESSION))
        for index in range(len(spool) - 1, -1, -1):
            yield np.frombuffer(spool.read(index), np.uint8).reshape(canvas.height, canvas.width, 3)


def _decode_segment(segment: Segment, canvas: Canvas, pad_rgb: tuple[int, int, int],
                    count: int) -> Iterator[np.ndarray]:
    command = [find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error",
               "-ss", f"{segment.start:.6f}", "-i", str(segment.path), "-map", "0:v:0",
               "-vf", canvas_filter(canvas, pad_rgb), "-frames:v", str(count),
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    produced = 0
    last: np.ndarray | None = None
    for frame in _pipe_frames(command, canvas.width, canvas.height, segment.path.name):
        produced += 1
        last = frame
        yield frame
    if last is None:
        raise MediaError(f"No frames could be decoded from {segment.path.name} "
                         f"at {segment.start:.2f}s.")
    # Short read at the end of a file: hold the last frame so timing stays exact.
    for _ in range(count - produced):
        yield last


def iter_timeline_frames(segments: Sequence[Segment], canvas: Canvas,
                         pad_rgb: tuple[int, int, int] = (0, 0, 0)) -> Iterator[np.ndarray]:
    for segment in segments:
        yield from iter_segment_frames(segment, canvas, pad_rgb)


def iter_frames(info: VideoInfo) -> Iterator[np.ndarray]:
    """Every frame of one clip at its own size and rate."""
    yield from iter_timeline_frames([Segment(info.path)], Canvas.of(info))


def _pipe_frames(command: list[str], width: int, height: int, name: str) -> Iterator[np.ndarray]:
    frame_bytes = width * height * 3
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log,
                                   stdin=subprocess.DEVNULL, **_SUBPROCESS)
        finished = False
        try:
            assert process.stdout is not None
            while True:
                raw = _read_exact(process.stdout, frame_bytes)
                if raw is None:
                    finished = True
                    break
                yield np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
        finally:
            process.stdout.close()  # type: ignore[union-attr]
            if not finished and process.poll() is None:
                process.kill()
            returncode = process.wait()
        if finished and returncode != 0:
            log.seek(0)
            details = log.read().decode(errors="replace").strip()[-2000:]
            raise MediaError(f"Decoding {name} failed: {details}")


def timeline_frame_count(segments: Sequence[Segment], canvas: Canvas, mirror_loop: bool) -> int:
    total = sum(segment.resolved().frame_count(canvas) for segment in segments)
    return mirrored_frame_count(total) if mirror_loop else total


def mirrored_frame_count(frame_count: int) -> int:
    """Frames in a forward-then-backward loop without repeated turnaround frames."""
    return frame_count + max(0, frame_count - 2)


# --------------------------------------------------------------------------- audio

def build_audio(segments: Sequence[Segment], canvas: Canvas, destination: Path,
                mirror_loop: bool) -> bool:
    """Render the timeline's audio to a WAV that lines up with the video frames.

    Clips without audio contribute silence. Returns False when no clip has audio.
    """
    resolved = [segment.resolved() for segment in segments]
    if not any(probe(segment.path).has_audio for segment in resolved):
        return False
    inputs: list[str] = []
    parts: list[str] = []
    total_frames = 0
    for index, segment in enumerate(resolved):
        frames = segment.frame_count(canvas)
        total_frames += frames
        seconds = frames / canvas.fps
        if probe(segment.path).has_audio:
            inputs += ["-ss", f"{segment.start:.6f}", "-t", f"{seconds + 0.05:.6f}",
                       "-i", str(segment.path)]
            chain = (f"[{index}:a:0]aresample={AUDIO_RATE},"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo,apad,"
                     f"atrim=end={seconds:.6f},asetpts=N/SR/TB")
        else:
            inputs += ["-f", "lavfi", "-t", f"{seconds:.6f}",
                       "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
            chain = (f"[{index}:a:0]aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"atrim=end={seconds:.6f},asetpts=N/SR/TB")
        if segment.reverse:
            chain += ",areverse,asetpts=N/SR/TB"
        parts.append(f"{chain}[a{index}]")
    labels = "".join(f"[a{index}]" for index in range(len(resolved)))
    graph = ";".join(parts) + f";{labels}concat=n={len(resolved)}:v=0:a=1[forward]"
    if mirror_loop and total_frames > 2:
        frame = 1.0 / canvas.fps
        back = (total_frames - 2) * frame
        graph += (f";[forward]asplit[f1][f2];[f2]areverse,asetpts=N/SR/TB,atrim=start={frame:.6f}:"
                  f"duration={back:.6f},asetpts=N/SR/TB[back];[f1][back]concat=n=2:v=0:a=1[out]")
    else:
        graph += ";[forward]anull[out]"
    _run([find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *inputs,
          "-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", str(destination)],
         "Building the audio track")
    return True


# --------------------------------------------------------------------------- encoding

def encoder_arguments(export_format: ExportFormat, webm_crf: int = DEFAULT_WEBM_CRF,
                      alpha: bool = True) -> list[str]:
    color_tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    if not alpha:
        return _plain_encoder_arguments(export_format, webm_crf, color_tags)
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
    if export_format == "mp4":
        raise MediaError("MP4 (H.264) cannot hold transparency; turn background removal off "
                         "or choose WebM or ProRes.")
    raise MediaError(f"Unknown export format {export_format!r}; use 'webm', 'prores' or 'mp4'.")


def _plain_encoder_arguments(export_format: str, webm_crf: int, color_tags: list[str]) -> list[str]:
    """Opaque output for pass-through exports (no background removal)."""
    # 4:2:0 formats need even dimensions; pad by one pixel if the source is odd.
    even = "pad=ceil(iw/2)*2:ceil(ih/2)*2"
    if export_format == "webm":
        return ["-vf", f"{even},scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
                "-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p",
                "-crf", str(int(webm_crf)), "-b:v", "0",
                "-row-mt", "1", "-deadline", "good", "-cpu-used", "4", *color_tags]
    if export_format == "prores":
        return ["-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv422p10le",
                "-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le",
                "-vendor", "apl0", *color_tags]
    if export_format == "mp4":
        return ["-vf", f"{even},scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
                "-c:v", "libx264", "-preset", "medium", "-crf", str(DEFAULT_MP4_CRF),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", *color_tags]
    raise MediaError(f"Unknown export format {export_format!r}; use 'webm', 'prores' or 'mp4'.")


def output_suffix(remove_background: bool = True, mirror_loop: bool = False) -> str:
    """File-name tag: name-alpha.webm, name-alpha-loop.webm, name-loop.mp4, ..."""
    parts = (["alpha"] if remove_background else []) + (["loop"] if mirror_loop else [])
    return "-" + "-".join(parts or ["export"])


def default_output_path(source: Path | str, export_format: ExportFormat,
                        remove_background: bool = True, mirror_loop: bool = False) -> Path:
    source = Path(source)
    tag = output_suffix(remove_background, mirror_loop)
    return source.with_name(f"{source.stem}{tag}{FORMATS[export_format]['suffix']}")


ProgressCallback = Callable[[int, int], None]


def export(source: Path | str, destination: Path | str, settings: KeySettings,
           export_format: ExportFormat, **options) -> Path:
    """Export one whole clip. See :func:`export_timeline` for the options."""
    return export_timeline([Segment(Path(source))], destination, settings, export_format,
                           **options)


def export_timeline(segments: Sequence[Segment], destination: Path | str, settings: KeySettings,
                    export_format: ExportFormat, *, webm_crf: int = DEFAULT_WEBM_CRF,
                    progress: ProgressCallback | None = None,
                    cancel: threading.Event | None = None,
                    mirror_loop: bool = False,
                    remove_background: bool = True,
                    include_audio: bool = False,
                    canvas: Canvas | None = None) -> Path:
    """Render ``segments`` back to back into ``destination``.

    Every segment is fitted onto ``canvas`` (default: the first clip's size and
    frame rate). With ``remove_background`` frames are keyed to alpha; without
    it they pass through and the output is a normal opaque video.

    With ``mirror_loop`` the whole timeline plays forward and then backward
    (frames 0..N-1 followed by N-2..1), so the last frame flows straight back
    into the first and the result loops seamlessly. The turnaround frames are
    not repeated, which would show up as a brief hold.
    """
    if not segments:
        raise MediaError("Add at least one clip to export.")
    if export_format not in FORMATS:
        raise MediaError(f"Unknown export format {export_format!r}; use 'webm', 'prores' or 'mp4'.")
    if remove_background and not FORMATS[export_format]["alpha"]:
        encoder_arguments(export_format, webm_crf, alpha=True)  # raises a clear error
    segments = [segment.resolved() for segment in segments]
    canvas = canvas or Canvas.of(probe(segments[0].path))
    destination = Path(destination)
    expected_suffix = str(FORMATS[export_format]["suffix"])
    if destination.suffix.lower() != expected_suffix:
        label = FORMATS[export_format]["label" if remove_background else "plain_label"]
        raise MediaError(f"{label} output must end in {expected_suffix}.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.stem}.partial{expected_suffix}")
    settings = settings.validated()
    pad_rgb = pad_color(settings, remove_background)

    with contextlib.ExitStack() as stack:
        audio_args: list[str] = []
        if include_audio:
            workdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="alphaloop-")))
            wav = workdir / "audio.wav"
            if build_audio(segments, canvas, wav, mirror_loop):
                audio_args = ["-i", str(wav), "-map", "0:v:0", "-map", "1:a:0",
                              *AUDIO_CODECS[export_format], "-shortest"]
        command = [find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "rawvideo", "-pix_fmt", "rgba" if remove_background else "rgb24",
                   "-s", f"{canvas.width}x{canvas.height}",
                   "-framerate", str(canvas.frame_rate), "-i", "-", *audio_args,
                   *([] if audio_args else ["-an"]),
                   *encoder_arguments(export_format, webm_crf, alpha=remove_background),
                   str(partial)]
        written = _encode(command, iter_timeline_frames(segments, canvas, pad_rgb), settings,
                          remove_background, mirror_loop,
                          timeline_frame_count(segments, canvas, mirror_loop),
                          partial, progress, cancel)
    os.replace(partial, destination)
    if progress is not None:
        progress(written, written)
    return destination


def _encode(command: list[str], frames: Iterator[np.ndarray], settings: KeySettings,
            remove_background: bool, mirror_loop: bool, expected: int, partial: Path,
            progress: ProgressCallback | None, cancel: threading.Event | None) -> int:
    written = 0
    with tempfile.TemporaryFile() as log, contextlib.ExitStack() as stack:
        spool = stack.enter_context(FrameSpool()) if mirror_loop else None
        encoder = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log,
                                   stdout=subprocess.DEVNULL, **_SUBPROCESS)
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

            for keyed, packed in _processed_frames(frames, settings, compress=mirror_loop,
                                                   key=remove_background):
                emit(keyed.tobytes())
                if spool is not None and packed is not None:
                    spool.append(packed)
            if spool is not None:
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
    return written


def _processed_frames(frames: Iterator[np.ndarray], settings: KeySettings, compress: bool = False,
                      key: bool = True) -> Iterator[tuple[np.ndarray, bytes | None]]:
    """Key frames on a small thread pool while preserving frame order.

    With ``compress`` each frame is also zlib-packed on the worker thread (zlib
    releases the GIL) for the mirror-loop spool. With ``key=False`` frames pass
    through unchanged.
    """
    def work(frame: np.ndarray) -> tuple[np.ndarray, bytes | None]:
        result = key_frame(frame, settings) if key else frame
        return result, (zlib.compress(result.tobytes(), SPOOL_COMPRESSION) if compress else None)

    workers = max(1, min(6, (os.cpu_count() or 2) - 1))
    try:
        if workers == 1:
            for frame in frames:
                yield work(frame)
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
    finally:
        close = getattr(frames, "close", None)
        if close is not None:
            close()


class FrameSpool:
    """Compressed frames on disk, so long clips can be replayed in reverse
    without holding them all in memory."""

    def __init__(self) -> None:
        self._file = tempfile.TemporaryFile(prefix="alphaloop-spool-")
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
        capture_output=True, check=False, stdin=subprocess.DEVNULL, **_SUBPROCESS,
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
        completed = subprocess.run(command, capture_output=True, text=True, check=False,
                                   stdin=subprocess.DEVNULL, **_SUBPROCESS)
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
