"""Fixed edge text bands per hospital, as fractions of the image: (x0, y0, x1, y1).

Only edge bands. Measured on the owner's real images (positions and content category only): the personal
data (chart number top-left, CJK name / hospital top-right) sits in the top band; the lower area holds only
technical parameters, which may stay visible. A thin bottom strip is masked as a precaution. Anything
else that looks like personal data is found by OCR and masked box by box (redact.py); the anatomy area is
never masked.

Override with KNEE_MASK_TEMPLATES='{"TSGH": [[0, 0, 1, 0.19], ...]}'.
"""

from __future__ import annotations

import json

from PIL import Image, ImageDraw

Rect = tuple[float, float, float, float]

DEFAULT_TEMPLATES: dict[str, list[Rect]] = {
    "TSGH": [
        (0.00, 0.000, 1.00, 0.19),  # top band: chart number, name, birth date, hospital
        (0.00, 0.955, 1.00, 1.00),  # bottom edge strip
    ],
    "VGHTPE": [
        (0.00, 0.000, 1.00, 0.17),
        (0.00, 0.955, 1.00, 1.00),
    ],
    # Studies without a hospital code in their names (the 2018 / 2023 X-rays): the hospital, patient name,
    # chart number, birth date, exam date and physician all sit in the top 0-11%.
    "": [
        (0.00, 0.000, 1.00, 0.13),
        (0.00, 0.955, 1.00, 1.00),
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

