import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from peak_alphaloop.keying import preset
from peak_alphaloop.media import (Canvas, Segment, build_audio, export_timeline,
                                  iter_timeline_frames, probe, read_frame, timeline_frame_count)

from test_media import HAS_FFMPEG, decode_all_rgba


def lavfi_clip(path: Path, video: str, audio: str | None = None, extra: list[str] | None = None) -> Path:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", video]
    if audio:
        command += ["-f", "lavfi", "-i", audio, "-c:a", "aac", "-shortest"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", *(extra or []), str(path)]
    subprocess.run(command, check=True)
    return path


def counter_clip(path: Path, size=(64, 48), rate=10, frames=10) -> Path:
    """Each frame's grey level encodes its index (frame k is 20*k + 10)."""
    width, height = size
    process = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{width}x{height}", "-r", str(rate), "-i", "-", "-c:v", "libx264", "-qp", "0",
         "-pix_fmt", "yuv444p", str(path)], stdin=subprocess.PIPE)
    assert process.stdin is not None
    for index in range(frames):
        process.stdin.write(bytes([20 * index + 10]) * (width * height))
    process.stdin.close()
    assert process.wait() == 0
    return path


def frame_ids(frames) -> list[int]:
    return [int(round((int(np.median(frame[..., 1])) - 10) / 20)) for frame in frames]


@unittest.skipUnless(HAS_FFMPEG, "FFmpeg is not installed")
class TimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def test_trim_reverse_and_order_are_frame_exact(self) -> None:
        clip = counter_clip(self.folder / "count.mp4")
        canvas = Canvas.of(probe(clip))
        segments = [Segment(clip, 0.2, 0.6), Segment(clip, 0.0, 0.3, reverse=True),
                    Segment(clip, 0.7)]
        frames = list(iter_timeline_frames(segments, canvas))
        self.assertEqual(frame_ids(frames), [2, 3, 4, 5, 2, 1, 0, 7, 8, 9])
        self.assertEqual(timeline_frame_count(segments, canvas, False), 10)
        self.assertEqual(timeline_frame_count(segments, canvas, True), 18)

    def test_mixed_sizes_and_rates_fit_the_first_clip(self) -> None:
        first = counter_clip(self.folder / "first.mp4", (64, 48), 10, 10)
        tall = counter_clip(self.folder / "tall.mp4", (24, 48), 20, 10)
        canvas = Canvas.of(probe(first))
        frames = list(iter_timeline_frames([Segment(first, 0, 0.5), Segment(tall, 0, 0.5)],
                                           canvas, (255, 0, 0)))
        self.assertEqual(len(frames), 10)                       # 0.5 s each at 10 fps
        self.assertTrue(all(f.shape == (48, 64, 3) for f in frames))
        last = frames[-1]
        np.testing.assert_array_equal(last[24, 2], (255, 0, 0))  # pillarbox in the pad color
        self.assertNotEqual(tuple(last[24, 32]), (255, 0, 0))

    def test_rotated_clip_is_upright(self) -> None:
        source = lavfi_clip(self.folder / "wide.mp4", "testsrc2=s=64x36:r=10:d=1")
        rotated = self.folder / "rotated.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-display_rotation", "90", "-i", str(source), "-c", "copy", str(rotated)],
                       check=True)
        info = probe(rotated)
        self.assertEqual((info.width, info.height), (36, 64))
        self.assertEqual(read_frame(rotated, 0.2).shape, (64, 36, 3))

    def test_audio_lines_up_with_video_including_mirror(self) -> None:
        with_audio = lavfi_clip(self.folder / "tone.mp4", "testsrc2=s=64x36:r=25:d=2",
                                "sine=f=440:d=2")
        silent = lavfi_clip(self.folder / "silent.mp4", "testsrc2=s=64x36:r=25:d=2")
        self.assertTrue(probe(with_audio).has_audio)
        self.assertFalse(probe(silent).has_audio)
        canvas = Canvas.of(probe(with_audio))
        segments = [Segment(with_audio, 0.4, 1.4), Segment(silent, 0, 0.6, reverse=True),
                    Segment(with_audio, 1.0, 1.8, reverse=True)]
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                wav = self.folder / f"audio-{mirror}.wav"
                self.assertTrue(build_audio(segments, canvas, wav, mirror))
                seconds = float(subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                     str(wav)], capture_output=True, text=True, check=True).stdout)
                expected = timeline_frame_count(segments, canvas, mirror) / canvas.fps
                self.assertAlmostEqual(seconds, expected, places=2)
        self.assertFalse(build_audio([Segment(silent)], canvas, self.folder / "none.wav", False))

    def test_export_timeline_with_audio_in_every_format(self) -> None:
        tone = lavfi_clip(self.folder / "tone.mp4", "testsrc2=s=64x36:r=25:d=1", "sine=f=440:d=1")
        other = lavfi_clip(self.folder / "other.mp4", "testsrc2=s=48x48:r=50:d=1")
        segments = [Segment(tone, 0, 0.6), Segment(other, 0.2, 0.6, reverse=True)]
        for export_format, key in (("mp4", False), ("webm", True), ("prores", True)):
            with self.subTest(format=export_format):
                suffix = {"mp4": ".mp4", "webm": ".webm", "prores": ".mov"}[export_format]
                output = self.folder / f"out{suffix}"
                export_timeline(segments, output, preset("soft"), export_format,
                                remove_background=key, include_audio=True, mirror_loop=True)
                frames = decode_all_rgba(output)
                self.assertEqual(len(frames), 25 + 23)  # 15 + 10 frames, mirrored
                self.assertEqual(frames.shape[1:3], (36, 64))
                streams = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of",
                     "csv=p=0", str(output)], capture_output=True, text=True, check=True).stdout
                self.assertIn("audio", streams)


if __name__ == "__main__":
    unittest.main()
