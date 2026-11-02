"""End-to-end check used by the release builds (``alphaloop --self-test REPORT``).

Confirms the FFmpeg that will actually be used has the encoders we need, then
keys, loops and exports a tiny generated clip in every format. Results are
written as JSON because windowed builds have no console to print to.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from . import __version__
from .keying import preset
from .media import (_SUBPROCESS, Segment, export_timeline, find_binary, probe,
                    read_alpha_range, timeline_frame_count, Canvas)

REQUIRED_ENCODERS = ("libvpx-vp9", "prores_ks", "libx264", "libopus", "aac", "pcm_s16le")


def run(report_path: Path | None = None) -> int:
    report: dict[str, object] = {"version": __version__, "python": sys.version.split()[0],
                                 "platform": platform.platform(), "frozen": bool(getattr(sys, "frozen", False)),
                                 "checks": {}}
    checks: dict[str, object] = report["checks"]  # type: ignore[assignment]
    started = time.monotonic()
    ok = True
    try:
        ffmpeg, ffprobe = find_binary("ffmpeg"), find_binary("ffprobe")
        checks["ffmpeg"] = ffmpeg
        checks["ffprobe"] = ffprobe
        version = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, **_SUBPROCESS).stdout.splitlines()
        checks["ffmpeg_version"] = version[0] if version else "unknown"
        encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                                  text=True, stdin=subprocess.DEVNULL, **_SUBPROCESS).stdout
        missing = [name for name in REQUIRED_ENCODERS if f" {name} " not in encoders]
        checks["missing_encoders"] = missing
        if missing:
            raise RuntimeError(f"FFmpeg is missing encoders: {', '.join(missing)}")

        with tempfile.TemporaryDirectory(prefix="alphaloop-selftest-") as folder:
            work = Path(folder)
            source = work / "source.mp4"
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "color=black:s=96x64:r=24:d=1,"
                            "drawbox=x=30:y=16:w=36:h=32:color=orange:t=fill",
                            "-f", "lavfi", "-i", "sine=f=440:d=1",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                            str(source)], check=True, capture_output=True,
                           stdin=subprocess.DEVNULL, **_SUBPROCESS)
            info = probe(source)
            segments = [Segment(source, 0, 0.5), Segment(source, 0.5, 1.0, reverse=True)]
            expected = timeline_frame_count(segments, Canvas.of(info), True)
            outputs = {}
            for export_format, suffix, key in (("webm", ".webm", True), ("prores", ".mov", True),
                                               ("mp4", ".mp4", False)):
                output = work / f"out{suffix}"
                export_timeline(segments, output, preset("hard"), export_format,  # type: ignore[arg-type]
                                remove_background=key, mirror_loop=True, include_audio=True)
                frames = probe(output).frame_count
                alpha = read_alpha_range(output) if key else None
                outputs[export_format] = {"bytes": output.stat().st_size, "frames": frames,
                                          "alpha_range": alpha}
                if key and alpha != (0, 255):
                    raise RuntimeError(f"{export_format}: expected alpha 0-255, got {alpha}")
                if abs(frames - expected) > 1:
                    raise RuntimeError(f"{export_format}: expected {expected} frames, got {frames}")
            checks["exports"] = outputs
    except Exception as error:  # report everything
        ok = False
        report["error"] = f"{type(error).__name__}: {error}"
        report["traceback"] = traceback.format_exc()
    report["ok"] = ok
    report["seconds"] = round(time.monotonic() - started, 2)
    text = json.dumps(report, indent=2)
    if report_path is not None:
        Path(report_path).write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        print(text)
    return 0 if ok else 1
