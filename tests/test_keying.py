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

    def test_rejects_wrong_input(self) -> None:
        with self.assertRaises(ValueError):
            key_frame(np.zeros((4, 4, 4), np.uint8), KeySettings())


if __name__ == "__main__":
    unittest.main()
