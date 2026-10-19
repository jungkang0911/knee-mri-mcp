"""Fixed mask regions per hospital, as fractions of the image: (x0, y0, x1, y1).

Measured from the text-box layout of the owner's real images (positions only): TSGH 2019 has text in the
top 0-18% across the full width, a dense block at the bottom left from 74%, a centered line around 76-80%,
a full-width line at 96-100%, and short labels at the left/right edges around 48-52% height. VGHTPE 2024
has text in the top 0-16% and at the bottom left from 72%, plus a full-width line at 96-100%.

The template is applied first; OCR masking and the OCR verification still run on the rest of the image.
Override with KNEE_MASK_TEMPLATES='{"TSGH": [[0, 0, 1, 0.2], ...]}'.
"""

from __future__ import annotations

import json

from PIL import Image, ImageDraw

Rect = tuple[float, float, float, float]

DEFAULT_TEMPLATES: dict[str, list[Rect]] = {
    "TSGH": [
        (0.00, 0.00, 1.00, 0.20),  # top band
        (0.00, 0.72, 0.40, 1.00),  # bottom-left block
        (0.00, 0.73, 0.75, 0.83),  # bottom centered line
        (0.00, 0.94, 1.00, 1.00),  # bottom full-width line
        (0.00, 0.44, 0.15, 0.57),  # left edge label
        (0.85, 0.44, 1.00, 0.57),  # right edge label
    ],
    "VGHTPE": [
        (0.00, 0.00, 1.00, 0.18),
        (0.00, 0.70, 0.40, 1.00),
        (0.00, 0.74, 0.75, 0.82),
        (0.00, 0.94, 1.00, 1.00),
    ],
}


def load_templates(env_value: str | None) -> dict[str, list[Rect]]:
    templates = dict(DEFAULT_TEMPLATES)
    if env_value:
        for hospital, rects in json.loads(env_value).items():
            templates[hospital.upper()] = [tuple(float(v) for v in r) for r in rects]  # type: ignore[misc]
    return templates


def apply_template(img: Image.Image, rects: list[Rect]) -> None:
    draw = ImageDraw.Draw(img)
    fill = 0 if img.mode in ("L", "1", "I", "F") else (0,) * len(img.getbands())
    w, h = img.size
    for x0, y0, x1, y1 in rects:
        draw.rectangle((int(x0 * w), int(y0 * h), int(x1 * w) - 1, int(y1 * h) - 1), fill=fill)


def inside_template(box: tuple[int, int, int, int], size: tuple[int, int], rects: list[Rect]) -> bool:
    """True when the pixel box lies completely inside one template rectangle (already blacked out)."""
    w, h = size
    x0, y0, x1, y1 = box
    return any(x0 >= r[0] * w and y0 >= r[1] * h and x1 <= r[2] * w + 1 and y1 <= r[3] * h + 1 for r in rects)
