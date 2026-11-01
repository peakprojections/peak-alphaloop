import unittest
from dataclasses import replace

import numpy as np

from peak_video_editor.keying import KeySettings, key_frame, preset


def reference_key(rgb: np.ndarray, settings: KeySettings) -> np.ndarray:
    """Straightforward float implementation the lookup-table keyer must match."""
    color = rgb.astype(np.float64) / 255.0
    if settings.background == "black":
        distance = color.max(axis=2)
    else:
        distance = 1.0 - color.min(axis=2)
    alpha = np.clip((distance - settings.threshold) / settings.softness, 0.0, 1.0)
    alpha_u8 = np.rint(alpha * 255.0)
    safe = np.maximum(alpha, 1e-6)[..., None]
    if settings.unblend:
        if settings.background == "black":
            foreground = color / safe
        else:
            foreground = (color - (1.0 - safe)) / safe
        foreground = np.clip(foreground, 0.0, 1.0)
        foreground[alpha_u8 == 255] = color[alpha_u8 == 255]
    else:
        foreground = color.copy()
    foreground[alpha_u8 == 0] = 0.0
    return np.dstack((np.rint(foreground * 255.0), alpha_u8)).astype(np.uint8)


class KeyFrameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.random = (np.random.default_rng(7).random((64, 96, 3)) * 255).astype(np.uint8)

    def test_matches_reference_math(self) -> None:
        for background in ("black", "white"):
            for name in ("soft", "hard"):
                for unblend in (True, False):
                    settings = replace(preset(name, background), unblend=unblend,
                                       protect_interior=False)
                    with self.subTest(background=background, preset=name, unblend=unblend):
                        np.testing.assert_array_equal(key_frame(self.random, settings),
                                                      reference_key(self.random, settings))

    def test_pure_background_is_transparent_and_subject_opaque(self) -> None:
        frame = np.zeros((10, 10, 3), np.uint8)
        frame[3:7, 3:7] = (255, 128, 0)
        keyed = key_frame(frame, preset("hard", "black"))
        self.assertEqual(keyed[0, 0, 3], 0)
        self.assertEqual(keyed[5, 5, 3], 255)
        np.testing.assert_array_equal(keyed[5, 5, :3], (255, 128, 0))

        white = np.full((10, 10, 3), 255, np.uint8)
        white[3:7, 3:7] = (0, 0, 200)
        keyed = key_frame(white, preset("hard", "white"))
        self.assertEqual(keyed[0, 0, 3], 0)
        self.assertEqual(keyed[5, 5, 3], 255)

    def test_soft_key_unblends_glow_back_to_full_color(self) -> None:
        # 50% orange over black should become orange at 50% opacity, not dark orange.
        frame = np.zeros((4, 4, 3), np.uint8)
        frame[:] = (128, 64, 0)
        keyed = key_frame(frame, KeySettings(threshold=0.0, softness=1.0))
        self.assertEqual(keyed[0, 0, 3], 128)
        np.testing.assert_allclose(keyed[0, 0, :3], (255, 128, 0), atol=1)

    def test_protect_interior_keeps_enclosed_dark_region_solid(self) -> None:
        frame = np.zeros((40, 40, 3), np.uint8)
        frame[10:30, 10:30] = 200
        frame[15:25, 15:25] = 3  # dark detail inside the subject
        without = key_frame(frame, replace(preset("hard"), protect_interior=False))
        with_protection = key_frame(frame, preset("hard"))
        self.assertEqual(without[20, 20, 3], 0)
        self.assertEqual(with_protection[20, 20, 3], 255)
        np.testing.assert_array_equal(with_protection[20, 20, :3], (3, 3, 3))
        self.assertEqual(with_protection[0, 0, 3], 0)

    def test_settings_from_query_strings(self) -> None:
        settings = KeySettings.from_dict({"background": "white", "threshold": "0.2",
                                          "softness": "0", "unblend": "false",
                                          "protect_interior": "true", "t": "1.5"})
        self.assertEqual(settings.background, "white")
        self.assertAlmostEqual(settings.threshold, 0.2)
        self.assertGreater(settings.softness, 0)
        self.assertFalse(settings.unblend)
        self.assertTrue(settings.protect_interior)
        with self.assertRaises(ValueError):
            KeySettings.from_dict({"background": "green"})

    def test_color_key_removes_uneven_screen_and_keeps_subject(self) -> None:
        screen = np.array((40, 170, 70), np.float32)
        frame = np.empty((60, 90, 3), np.float32)
        frame[:] = screen
        frame[:, :30] *= 0.8          # shadowed part of the screen
        frame[:, 60:] *= 1.12         # brighter part of the screen
        frame[20:40, 35:55] = (220, 170, 140)   # skin
        frame[25:30, 40:45] = (25, 22, 20)      # dark detail
        frame = np.clip(frame, 0, 255).astype(np.uint8)
        for name in ("soft", "hard"):
            with self.subTest(preset=name):
                keyed = key_frame(frame, preset(name, "color", "#28aa46"))
                self.assertEqual(keyed[5, 5, 3], 0)
                self.assertEqual(keyed[5, 45, 3], 0)
                self.assertEqual(keyed[5, 80, 3], 0)
                self.assertEqual(keyed[35, 50, 3], 255)
                self.assertEqual(keyed[27, 42, 3], 255)
                np.testing.assert_array_equal(keyed[35, 50, :3], (220, 170, 140))

    def test_color_key_unblends_and_despills_edges(self) -> None:
        key = np.array((30, 160, 60), np.float64)
        subject = np.array((200, 120, 100), np.float64)
        frame = np.empty((4, 4, 3), np.uint8)
        frame[:] = np.rint(0.5 * subject + 0.5 * key)
        settings = KeySettings(background="color", key_color="#1ea03c",
                               threshold=0.0, softness=0.6, despill=False)
        keyed = key_frame(frame, settings)
        self.assertTrue(0 < keyed[0, 0, 3] < 255)
        # Unblending moves the edge color away from the green key toward the subject.
        self.assertLess(int(keyed[0, 0, 1]), int(frame[0, 0, 1]))
        self.assertGreater(int(keyed[0, 0, 0]), int(frame[0, 0, 0]))

        spilled = np.zeros((4, 4, 3), np.uint8)
        spilled[:] = (200, 190, 120)   # skin with green bounce
        cleaned = key_frame(spilled, preset("hard", "color", "#1ea03c"))
        self.assertEqual(cleaned[0, 0, 3], 255)
        self.assertEqual(cleaned[0, 0, 1], 160)   # limited to mean of red and blue
        untouched = key_frame(spilled, replace(preset("hard", "color", "#1ea03c"), despill=False))
        self.assertEqual(untouched[0, 0, 1], 190)

    def test_neutral_key_color_skips_despill(self) -> None:
        frame = np.zeros((4, 4, 3), np.uint8)
        frame[:] = (90, 200, 90)
        keyed = key_frame(frame, preset("hard", "color", "#808080"))
        self.assertEqual(keyed[0, 0, 1], 200)

    def test_color_settings_validation(self) -> None:
        settings = KeySettings.from_dict({"background": "color", "key_color": "00B140",
                                          "despill": "false"})
        self.assertEqual(settings.key_color, "#00b140")
        self.assertEqual(settings.key_rgb, (0, 177, 64))
        self.assertFalse(settings.despill)
        with self.assertRaises(ValueError):
            KeySettings.from_dict({"background": "color", "key_color": "green"})

    def test_rejects_wrong_input(self) -> None:
        with self.assertRaises(ValueError):
            key_frame(np.zeros((4, 4, 4), np.uint8), KeySettings())


if __name__ == "__main__":
    unittest.main()
