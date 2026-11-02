"""Draw the Peak Alphaloop icon (a loop over a transparency checkerboard)."""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def draw(size: int = 1024) -> Image.Image:
    scale = 4  # supersample for smooth edges
    s = size * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin, radius = int(s * 0.06), int(s * 0.2)
    box = (margin, margin, s - margin, s - margin)
    draw.rounded_rectangle(box, radius, fill=(16, 21, 30, 255))

    # Checkerboard "transparency" band across the middle.
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius, fill=255)
    checker = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    cdraw = ImageDraw.Draw(checker)
    cell = s // 16
    top, bottom = int(s * 0.30), int(s * 0.70)
    for y in range(top, bottom, cell):
        for x in range(0, s, cell):
            shade = (54, 62, 77, 255) if ((x // cell) + (y // cell)) % 2 else (38, 45, 57, 255)
            cdraw.rectangle((x, y, x + cell, min(y + cell, bottom)), fill=shade)
    image.paste(checker, (0, 0), Image.composite(checker, Image.new("RGBA", (s, s)), mask))

    # Lemniscate (infinity loop) stroke with a blue-to-green gradient.
    points = []
    for step in range(721):
        t = 2 * math.pi * step / 720
        denominator = 1 + math.sin(t) ** 2
        x = math.cos(t) / denominator
        y = math.sin(t) * math.cos(t) / denominator
        points.append((s / 2 + x * s * 0.36, s / 2 + y * s * 0.36))
    width = int(s * 0.075)
    for index in range(len(points) - 1):
        f = 1 - abs(2 * index / (len(points) - 1) - 1)  # blue -> green -> blue, no seam
        color = (int(74 + (70 - 74) * f), int(168 + (194 - 168) * f), int(255 + (139 - 255) * f), 255)
        draw.line([points[index], points[index + 1]], fill=color, width=width)
        draw.ellipse([points[index][0] - width / 2, points[index][1] - width / 2,
                      points[index][0] + width / 2, points[index][1] + width / 2], fill=color)
    return image.resize((size, size), Image.LANCZOS)


def main(out_dir: str) -> None:
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    icon = draw()
    icon.save(folder / "icon.png")
    icon.save(folder / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                         (128, 128), (256, 256)])
    icon.save(folder / "icon.icns")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "build/icon")
