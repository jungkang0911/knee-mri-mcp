"""Cropping, zooming, contact sheets, side-by-side comparisons and size-capped PNG encoding."""

from __future__ import annotations

import io
import math
from collections.abc import Sequence

from PIL import Image, ImageDraw, ImageFont

from .errors import KneeError

BG = 24
FG = 235
PENDING = "pending"  # contact-sheet tile placeholder for an image that is still being masked


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def apply_crop(img: Image.Image, crop: Sequence[int] | None) -> Image.Image:
    if crop is None:
        return img
    if len(crop) != 4:
        raise KneeError("crop must be [x0, y0, x1, y1] in original-image pixels")
    x0, y0, x1, y1 = (int(v) for v in crop)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.width, x1), min(img.height, y1)
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise KneeError(f"crop is empty or too small; image is {img.width}x{img.height}")
    return img.crop((x0, y0, x1, y1))


def apply_zoom(img: Image.Image, zoom: float) -> Image.Image:
    if not 0.25 <= zoom <= 4:
        raise KneeError("zoom must be between 0.25 and 4")
    if zoom == 1:
        return img
    return img.resize((max(1, round(img.width * zoom)), max(1, round(img.height * zoom))), Image.LANCZOS)


def to_png(img: Image.Image, max_bytes: int) -> bytes:
    """PNG no larger than max_bytes, downscaling when needed."""
    cur = img
    for _ in range(12):
        buf = io.BytesIO()
        cur.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data
        ratio = max(0.5, min(0.9, math.sqrt(max_bytes / len(data)) * 0.95))
        cur = cur.resize((max(1, int(cur.width * ratio)), max(1, int(cur.height * ratio))), Image.LANCZOS)
    raise KneeError("could not encode the image under the size limit")


def _label_bar(width: int, text: str, size: int) -> Image.Image:
    bar = Image.new("L", (width, size + 8), BG)
    ImageDraw.Draw(bar).text((4, 3), text, fill=FG, font=_font(size))
    return bar


def contact_sheet(tiles: list[tuple[str, Image.Image | None]], cols: int, thumb: int = 180) -> Image.Image:
    """Grid of thumbnails in the given order, each labelled. A None image renders as a withheld tile."""
    if not 1 <= cols <= 10:
        raise KneeError("cols must be between 1 and 10")
    rows = max(1, math.ceil(len(tiles) / cols))
    label_h = 22
    pad = 4
    sheet = Image.new("L", (cols * (thumb + pad) + pad, rows * (thumb + label_h + pad) + pad), BG)
    draw = ImageDraw.Draw(sheet)
    font = _font(16)
    for n, (label, img) in enumerate(tiles):
        r, c = divmod(n, cols)
        x = pad + c * (thumb + pad)
        y = pad + r * (thumb + label_h + pad)
        draw.text((x + 3, y + 2), label, fill=FG, font=font)
        if img is None or isinstance(img, str):
            draw.rectangle((x, y + label_h, x + thumb - 1, y + label_h + thumb - 1), outline=90)
            text = "masking..." if img == PENDING else "withheld"
            draw.text((x + 8, y + label_h + thumb // 2 - 8), text, fill=140, font=font)
            continue
        t = img.convert("L").copy()
        t.thumbnail((thumb, thumb), Image.LANCZOS)
        sheet.paste(t, (x + (thumb - t.width) // 2, y + label_h + (thumb - t.height) // 2))
    return sheet


def side_by_side(a: Image.Image, a_label: str, b: Image.Image, b_label: str, height: int = 640) -> Image.Image:
    def fit(im: Image.Image) -> Image.Image:
        im = im.convert("L")
        h = min(height, max(im.height, 256))
        return im.resize((max(1, round(im.width * h / im.height)), h), Image.LANCZOS)

    a2, b2 = fit(a), fit(b)
    h = max(a2.height, b2.height)
    gap, label = 12, 30
    out = Image.new("L", (a2.width + b2.width + gap, h + label), BG)
    out.paste(_label_bar(a2.width, a_label, 20), (0, 0))
    out.paste(_label_bar(b2.width, b_label, 20), (a2.width + gap, 0))
    out.paste(a2, (0, label))
    out.paste(b2, (a2.width + gap, label))
    return out
