"""Background keying: black, white, or any picked color (green screen etc.).

Every pixel is modelled as ``observed = alpha * foreground + (1 - alpha) * background``.
Alpha is derived from how far the pixel is from the background color, and the
foreground color is then recovered ("unblended") so semi-transparent edges, glows
and smoke do not keep a fringe of the background color once they are composited
over something else.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from typing import Any, Literal

import numpy as np

Background = Literal["black", "white", "color"]

MIN_SOFTNESS = 0.001
HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")

# Color-key distance is measured in YCbCr. Brightness differences count for less
# than hue/saturation differences so shadows and uneven lighting on a green screen
# still read as "screen". Rows give weighted (Y, Cb, Cr) from RGB in 0-1.
_LUMA_WEIGHT = 0.5
_YCBCR = np.array([
    [0.299, 0.587, 0.114],
    [-0.168736, -0.331264, 0.5],
    [0.5, -0.418688, -0.081312],
], dtype=np.float64)
_WEIGHTED = _YCBCR * np.array([[_LUMA_WEIGHT], [1.0], [1.0]])
# Quadratic form: distance^2 = d^T Q d for an RGB difference d.
_DISTANCE_Q = (_WEIGHTED.T @ _WEIGHTED).astype(np.float32)
# A key color needs at least this much saturation before spill suppression applies.
_DESPILL_MIN_SATURATION = 0.15


@dataclass(frozen=True)
class KeySettings:
    """Controls for one key.

    background: "black", "white", or "color" (uses ``key_color``).
    key_color:  "#rrggbb" background color for the "color" mode, e.g. a sampled
                green screen.
    threshold:  distance from the background (0-1) that is still fully transparent.
                Raise it to swallow noise, shadows or an uneven screen.
    softness:   distance range (0-1) over which pixels ramp from transparent to
                opaque. 1.0 with threshold 0 is a pure luma key on black/white
                (best for glows, smoke, light, particles); small values give a
                hard cutout.
    unblend:    recover true foreground color on semi-transparent pixels, which
                removes fringes of the background color. Leave on unless colors
                look wrong.
    protect_interior: only key background-colored regions connected to the frame
                edge, so parts of the subject that match the background stay solid.
                Best for solid objects, not for smoke or particles.
    despill:    for color keys, pull the key's color cast (green bounce on skin
                and hair) out of the subject. Ignored for black/white.
    """

    background: Background = "black"
    threshold: float = 0.03
    softness: float = 0.97
    unblend: bool = True
    protect_interior: bool = False
    key_color: str = "#00b140"
    despill: bool = True

    def validated(self) -> "KeySettings":
        if self.background not in ("black", "white", "color"):
            raise ValueError("background must be 'black', 'white' or 'color'.")
        match = HEX_COLOR.match(str(self.key_color).strip())
        if not match:
            raise ValueError("key_color must be a #rrggbb hex color.")
        threshold = float(np.clip(self.threshold, 0.0, 1.0))
        softness = float(np.clip(self.softness, MIN_SOFTNESS, 1.0))
        return replace(self, threshold=threshold, softness=softness,
                       key_color="#" + match.group(1).lower(),
                       unblend=bool(self.unblend),
                       protect_interior=bool(self.protect_interior),
                       despill=bool(self.despill))

    @property
    def key_rgb(self) -> tuple[int, int, int]:
        value = HEX_COLOR.match(self.key_color.strip())
        if not value:
            raise ValueError("key_color must be a #rrggbb hex color.")
        number = int(value.group(1), 16)
        return (number >> 16) & 255, (number >> 8) & 255, number & 255

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KeySettings":
        known = {key: data[key] for key in cls.__dataclass_fields__ if key in data}
        for key in ("threshold", "softness"):
            if key in known:
                known[key] = float(known[key])
        for key in ("unblend", "protect_interior", "despill"):
            if key in known and isinstance(known[key], str):
                known[key] = known[key].strip().lower() in {"1", "true", "yes", "on"}
        return cls(**known).validated()


PRESETS: dict[str, KeySettings] = {
    # Additive content shot on black: light, fire, smoke, sparks, particles.
    "soft": KeySettings(threshold=0.03, softness=0.97, unblend=True, protect_interior=False),
    # Solid subjects (logos, products, people) on a flat background.
    "hard": KeySettings(threshold=0.08, softness=0.10, unblend=True, protect_interior=True),
}

# Color keys measure a different distance, so they need their own starting points.
COLOR_PRESETS: dict[str, KeySettings] = {
    # Hair, motion blur, translucent edges on a green/blue screen.
    "soft": KeySettings(background="color", threshold=0.10, softness=0.14,
                        unblend=True, protect_interior=False, despill=True),
    # Crisp edges for solid subjects on an evenly lit screen.
    "hard": KeySettings(background="color", threshold=0.14, softness=0.05,
                        unblend=True, protect_interior=True, despill=True),
}


def preset(name: str, background: Background = "black",
           key_color: str | None = None) -> KeySettings:
    table = COLOR_PRESETS if background == "color" else PRESETS
    try:
        chosen = replace(table[name], background=background)
    except KeyError as error:
        raise ValueError(f"Unknown preset {name!r}; choose from {sorted(PRESETS)}.") from error
    if key_color is not None:
        chosen = replace(chosen, key_color=key_color)
    return chosen.validated()


def key_frame(rgb: np.ndarray, settings: KeySettings) -> np.ndarray:
    """Key one frame. ``rgb`` is HxWx3 uint8; returns HxWx4 uint8 (straight alpha).

    Alpha depends only on the pixel's max channel (black) or min channel (white),
    and each recovered channel depends only on that value and the channel itself,
    so the whole key is two small lookup tables applied with ``take``.
    """
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
        raise ValueError("key_frame expects an HxWx3 uint8 array.")
    settings = settings.validated()
    if settings.background == "color":
        output = _key_color(rgb, settings)
        _apply_protect_interior(output, rgb, settings)
        return output
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
    _apply_protect_interior(output, rgb, settings)
    return output


def _apply_protect_interior(output: np.ndarray, rgb: np.ndarray, settings: KeySettings) -> None:
    if not settings.protect_interior:
        return
    interior = _interior_mask(output[..., 3])
    if interior is not None:
        output[..., 3][interior] = 255
        output[..., :3][interior] = _despilled(rgb, settings)[interior]


def _key_color(rgb: np.ndarray, settings: KeySettings) -> np.ndarray:
    """Key an arbitrary background color (green screen, blue screen, grey wall...).

    Alpha comes from a 16M-entry table indexed by the packed RGB value; each
    recovered channel then depends only on alpha and that channel's own value,
    so it is a 256x256 table per channel.
    """
    alpha_lut = _color_alpha_table(settings.key_color, settings.threshold, settings.softness)
    channel_luts = _color_channel_tables(settings.key_color, settings.unblend)

    packed = rgb[..., 0].astype(np.uint32) << 16
    packed |= rgb[..., 1].astype(np.uint32) << 8
    packed |= rgb[..., 2]
    output = np.empty(rgb.shape[:2] + (4,), dtype=np.uint8)
    alpha = alpha_lut.take(packed)
    output[..., 3] = alpha
    high = alpha.astype(np.uint16) << 8
    for channel in range(3):
        output[..., channel] = channel_luts[channel].take(high | rgb[..., channel])

    spill = _spill_channel(settings)
    if spill is not None:
        a, b = [i for i in range(3) if i != spill]
        np.minimum(output[..., spill], _channel_mean(output[..., a], output[..., b]),
                   out=output[..., spill])
    return output


@lru_cache(maxsize=4)
def _color_alpha_table(key_color: str, threshold: float, softness: float) -> np.ndarray:
    key = np.array(KeySettings(key_color=key_color).key_rgb, dtype=np.float64) / 255.0
    values = np.arange(256, dtype=np.float64) / 255.0
    q = _DISTANCE_Q.astype(np.float64)
    dg = (values - key[1])[:, None]
    db = (values - key[2])[None, :]
    green_blue = q[1, 1] * dg * dg + q[2, 2] * db * db + 2.0 * q[1, 2] * dg * db
    cross_g, cross_b = 2.0 * q[0, 1] * dg, 2.0 * q[0, 2] * db
    table = np.empty(1 << 24, dtype=np.uint8)
    for red in range(256):
        dr = values[red] - key[0]
        distance_sq = green_blue + q[0, 0] * dr * dr + dr * (cross_g + cross_b)
        alpha = np.clip((np.sqrt(np.maximum(distance_sq, 0.0)) - threshold) / softness, 0.0, 1.0)
        table[red << 16:(red + 1) << 16] = np.rint(alpha * 255.0).ravel()
    table.flags.writeable = False
    return table


@lru_cache(maxsize=8)
def _color_channel_tables(key_color: str, unblend: bool) -> tuple[np.ndarray, ...]:
    """Per channel: value[alpha << 8 | observed] -> recovered foreground (uint8)."""
    key = np.array(KeySettings(key_color=key_color).key_rgb, dtype=np.float64) / 255.0
    alpha = (np.arange(256, dtype=np.float64) / 255.0)[:, None]
    observed = (np.arange(256, dtype=np.float64) / 255.0)[None, :]
    tables = []
    for channel in range(3):
        if unblend:
            safe = np.maximum(alpha, 1e-6)
            value = np.clip(key[channel] + (observed - key[channel]) / safe, 0.0, 1.0)
            value = np.where(alpha >= 1.0, observed, value)
        else:
            value = np.broadcast_to(observed, (256, 256))
        value = np.where(alpha <= 0.0, 0.0, value)
        table = np.rint(value * 255.0).astype(np.uint8).ravel()
        table.flags.writeable = False
        tables.append(table)
    return tuple(tables)


def _channel_mean(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """Rounded mean of two uint8 channels without overflow.

    Limiting the spill channel to the mean of the other two is the classic
    "average" despill: it neutralises green bounce on skin, hair and white
    edges. Saturated colors close to the key (yellows, teals) shift a little;
    turn despill off if the subject really contains them.
    """
    return ((first.astype(np.uint16) + second + 1) >> 1).astype(np.uint8)


def _spill_channel(settings: KeySettings) -> int | None:
    """Index of the key color's dominant channel, if the key is saturated enough."""
    if settings.background != "color" or not settings.despill:
        return None
    key = np.array(settings.key_rgb, dtype=np.float32) / 255.0
    if float(key.max() - key.min()) < _DESPILL_MIN_SATURATION:
        return None
    dominant = int(np.argmax(key))
    others = sorted(key[i] for i in range(3) if i != dominant)
    # Two-channel keys (cyan, magenta, yellow...) have no single spill channel.
    if key[dominant] - others[-1] < _DESPILL_MIN_SATURATION / 2:
        return None
    return dominant


def _despilled(rgb: np.ndarray, settings: KeySettings) -> np.ndarray:
    channel = _spill_channel(settings)
    if channel is None:
        return rgb
    a, b = [i for i in range(3) if i != channel]
    result = rgb.copy()
    np.minimum(result[..., channel], _channel_mean(rgb[..., a], rgb[..., b]),
               out=result[..., channel])
    return result


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
