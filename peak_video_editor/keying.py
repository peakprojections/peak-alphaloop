"""Black/white background keying.

Every pixel is modelled as ``observed = alpha * foreground + (1 - alpha) * background``
where the background is pure black or pure white. Alpha is derived from how far the
pixel is from the background color, and the foreground color is then recovered
("unblended") so semi-transparent edges, glows and smoke do not keep a dark or
light fringe once they are composited over something else.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from typing import Any, Literal

import numpy as np

Background = Literal["black", "white"]

MIN_SOFTNESS = 0.001


@dataclass(frozen=True)
class KeySettings:
    """Controls for one key.

    background: which background color becomes transparent.
    threshold:  distance from the background (0-1) that is still fully transparent.
                Raise it to swallow compression noise near pure black/white.
    softness:   distance range (0-1) over which pixels ramp from transparent to
                opaque. 1.0 with threshold 0 is a pure luma key (best for glows,
                smoke, light, particles); small values give a hard cutout.
    unblend:    recover true foreground color on semi-transparent pixels, which
                removes dark/light fringes. Leave on unless colors look wrong.
    protect_interior: only key background-colored regions connected to the frame
                edge, so dark parts of a subject on black (or light parts on white)
                stay solid. Best for solid objects, not for smoke or particles.
    """

    background: Background = "black"
    threshold: float = 0.03
    softness: float = 0.97
    unblend: bool = True
    protect_interior: bool = False

    def validated(self) -> "KeySettings":
        if self.background not in ("black", "white"):
            raise ValueError("background must be 'black' or 'white'.")
        threshold = float(np.clip(self.threshold, 0.0, 1.0))
        softness = float(np.clip(self.softness, MIN_SOFTNESS, 1.0))
        return replace(self, threshold=threshold, softness=softness,
                       unblend=bool(self.unblend),
                       protect_interior=bool(self.protect_interior))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KeySettings":
        known = {key: data[key] for key in cls.__dataclass_fields__ if key in data}
        for key in ("threshold", "softness"):
            if key in known:
                known[key] = float(known[key])
        for key in ("unblend", "protect_interior"):
            if key in known and isinstance(known[key], str):
                known[key] = known[key].strip().lower() in {"1", "true", "yes", "on"}
        return cls(**known).validated()


PRESETS: dict[str, KeySettings] = {
    # Additive content shot on black: light, fire, smoke, sparks, particles.
    "soft": KeySettings(threshold=0.03, softness=0.97, unblend=True, protect_interior=False),
    # Solid subjects (logos, products, people) on a flat background.
    "hard": KeySettings(threshold=0.08, softness=0.10, unblend=True, protect_interior=True),
}


def preset(name: str, background: Background = "black") -> KeySettings:
    try:
        return replace(PRESETS[name], background=background)
    except KeyError as error:
        raise ValueError(f"Unknown preset {name!r}; choose from {sorted(PRESETS)}.") from error


def key_frame(rgb: np.ndarray, settings: KeySettings) -> np.ndarray:
    """Key one frame. ``rgb`` is HxWx3 uint8; returns HxWx4 uint8 (straight alpha).

    Alpha depends only on the pixel's max channel (black) or min channel (white),
    and each recovered channel depends only on that value and the channel itself,
    so the whole key is two small lookup tables applied with ``take``.
    """
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
        raise ValueError("key_frame expects an HxWx3 uint8 array.")
    settings = settings.validated()
    alpha_lut, color_lut = _tables(settings)

    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    if settings.background == "black":
        reference = np.maximum(np.maximum(red, green), blue)
    else:
        reference = np.minimum(np.minimum(red, green), blue)

    output = np.empty(rgb.shape[:2] + (4,), dtype=np.uint8)
    output[..., 3] = alpha_lut.take(reference)
    index = (reference.astype(np.uint16) << 8)[..., None] | rgb
    output[..., :3] = color_lut.take(index)

    if settings.protect_interior:
        interior = _interior_mask(output[..., 3])
        if interior is not None:
            output[..., 3][interior] = 255
            output[..., :3][interior] = rgb[interior]
    return output


@lru_cache(maxsize=32)
def _tables(settings: KeySettings) -> tuple[np.ndarray, np.ndarray]:
    """Return (alpha[reference], color[reference << 8 | channel]) as uint8 tables."""
    values = np.arange(256, dtype=np.float64) / 255.0
    if settings.background == "black":
        distance = values
    else:
        distance = 1.0 - values
    alpha = np.clip((distance - settings.threshold) / settings.softness, 0.0, 1.0)
    alpha_u8 = np.rint(alpha * 255.0).astype(np.uint8)

    channel = values[None, :]
    a = alpha[:, None]
    if settings.unblend:
        safe = np.maximum(a, 1e-6)
        if settings.background == "black":
            color = channel / safe
        else:
            color = (channel - (1.0 - safe)) / safe
        color = np.clip(color, 0.0, 1.0)
        # Fully opaque pixels keep the source value exactly.
        color = np.where(alpha_u8[:, None] == 255, channel, color)
    else:
        color = np.broadcast_to(channel, (256, 256))
    # Fully transparent pixels carry no color; zero them so encoders waste no bits.
    color = np.where(alpha_u8[:, None] == 0, 0.0, color)
    color_u8 = np.rint(color * 255.0).astype(np.uint8).ravel()
    alpha_u8.flags.writeable = False
    color_u8.flags.writeable = False
    return alpha_u8, color_u8


def _interior_mask(alpha: np.ndarray) -> np.ndarray | None:
    """See-through pixels that are not connected to the frame edge, or None."""
    from scipy import ndimage

    candidate = alpha < 255
    if not candidate.any():
        return None
    labels, count = ndimage.label(candidate, structure=np.ones((3, 3), dtype=bool))
    if count == 0:
        return None
    edge_labels = np.unique(np.concatenate((
        labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1],
    )))
    keep_outside = np.zeros(count + 1, dtype=bool)
    keep_outside[edge_labels] = True
    keep_outside[0] = True  # opaque pixels are not interior holes
    interior = ~keep_outside[labels]
    return interior if interior.any() else None


def alpha_matte(rgba: np.ndarray) -> np.ndarray:
    """Grayscale view of the alpha channel as HxWx3 uint8 (white = opaque)."""
    return np.repeat(rgba[..., 3:4], 3, axis=2)
