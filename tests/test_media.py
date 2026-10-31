import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from peak_video_editor.keying import preset
from peak_video_editor.media import (ExportCancelled, MediaError, default_output_path, export,
                                     probe, read_alpha_range, read_frame)

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_clip(path: Path, background: int = 0, frames: int = 12, size=(160, 96)) -> Path:
    width, height = size
    process = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", "24", "-i", "-",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "12", str(path)],
        stdin=subprocess.PIPE,
    )
    assert process.stdin is not None
    for index in range(frames):
        frame = np.full((height, width, 3), background, np.uint8)
        frame[30:66, 40 + index * 2:100 + index * 2] = (230, 120, 20)
        process.stdin.write(frame.tobytes())
    process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError("could not build test clip")
    return path


def decode_rgba(path: Path, index: int = 0) -> np.ndarray:
    info = probe(path)
    decoder = ["-c:v", "libvpx-vp9"] if path.suffix == ".webm" else []
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *decoder, "-i", str(path),
         "-vf", f"select=eq(n\\,{index}),format=rgba", "-frames:v", "1",
         "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(completed.stdout, np.uint8).reshape(info.height, info.width, 4)


@unittest.skipUnless(HAS_FFMPEG, "FFmpeg is not installed")
class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def test_probe_and_read_frame(self) -> None:
        clip = make_clip(self.folder / "clip.mp4")
        info = probe(clip)
        self.assertEqual((info.width, info.height, info.frame_count), (160, 96, 12))
        self.assertEqual(float(info.frame_rate), 24.0)
        frame = read_frame(clip, 0.2, max_width=80)
        self.assertEqual(frame.shape, (48, 80, 3))

    def test_export_both_formats_keep_alpha(self) -> None:
        for background, name in ((0, "black"), (255, "white")):
            clip = make_clip(self.folder / f"{name}.mp4", background)
            for export_format in ("webm", "prores"):
                with self.subTest(background=name, format=export_format):
                    output = default_output_path(clip, export_format)
                    calls = []
                    export(clip, output, preset("hard", name), export_format,
                           progress=lambda done, total: calls.append((done, total)))
                    self.assertTrue(output.is_file())
                    self.assertEqual(calls[-1][0], 12)
                    self.assertEqual(read_alpha_range(output), (0, 255))
                    rgba = decode_rgba(output)
                    self.assertLess(rgba[5, 5, 3], 8, "background should be transparent")
                    self.assertGreater(rgba[48, 70, 3], 245, "subject should be opaque")
                    self.assertFalse(any(self.folder.glob(".*partial*")))

    def test_native_vp9_decoder_drops_alpha(self) -> None:
        # Documents why WebM consumers must decode with libvpx-vp9.
        clip = make_clip(self.folder / "clip.mp4")
        output = default_output_path(clip, "webm")
        export(clip, output, preset("hard"), "webm")
        completed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(output), "-frames:v", "1",
             "-vf", "format=rgba,alphaextract", "-f", "rawvideo", "-"],
            capture_output=True, check=True)
        self.assertEqual(min(completed.stdout), 255)

    def test_cancel_removes_partial_output(self) -> None:
        clip = make_clip(self.folder / "clip.mp4", frames=24)
        cancel = threading.Event()
        cancel.set()
        output = self.folder / "out.webm"
        with self.assertRaises(ExportCancelled):
            export(clip, output, preset("soft"), "webm", cancel=cancel)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.folder.glob(".*partial*")), [])

    def test_wrong_suffix_and_missing_file(self) -> None:
        clip = make_clip(self.folder / "clip.mp4")
        with self.assertRaises(MediaError):
            export(clip, self.folder / "out.mp4", preset("soft"), "webm")
        with self.assertRaises(MediaError):
            probe(self.folder / "missing.mp4")


if __name__ == "__main__":
    unittest.main()
