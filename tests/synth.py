"""Synthetic knee-like images with fake burned-in patient details (no real data)."""

from __future__ import annotations

import io
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FAKE_NAME = "WANG XIAOMING"
FAKE_MRN = "MRN 20481736"
FAKE_DOB = "DOB 1975/04/12"
FAKE_DOCTOR = "Dr. CHEN KUOHUA"
FAKE_PII_TOKENS = ["WANG", "XIAOMING", "20481736", "1975", "CHEN", "KUOHUA"]


def _font(size: int):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def knee_image(
    index: int = 1,
    plane: str = "COR PD",
    sl: str = "-12.5",
    st: str = "3.0",
    pii: bool = True,
    size: int = 512,
    seed: int | None = None,
    cjk_name: str | None = None,
) -> bytes:
    rnd = random.Random(seed if seed is not None else index)
    img = Image.new("L", (size, size), 8)
    d = ImageDraw.Draw(img)
    # Fake anatomy: femur / tibia blobs with texture.
    d.ellipse((150, 60, 360, 250), fill=120)
    d.ellipse((160, 270, 350, 470), fill=110)
    d.rectangle((215, 230, 300, 290), fill=60)
    for _ in range(2500):
        x, y = rnd.randrange(140, 370), rnd.randrange(60, 470)
        img.putpixel((x, y), max(0, min(255, img.getpixel((x, y)) + rnd.randint(-40, 40))))
    img = img.filter(ImageFilter.GaussianBlur(1.2))
    d = ImageDraw.Draw(img)
    f = _font(15)
    if pii:
        d.text((8, 6), FAKE_NAME, fill=230, font=f)
        d.text((8, 26), FAKE_MRN, fill=230, font=f)
        d.text((8, 46), FAKE_DOB, fill=230, font=f)
        d.text((size - 150, 6), FAKE_DOCTOR, fill=230, font=f)
    if cjk_name:
        d.text((8, 66), cjk_name, fill=230, font=ImageFont.truetype("msjh.ttc", 18))
    d.text((size - 90, 26), "TSGH", fill=230, font=f)
    d.text((8, size - 66), plane, fill=230, font=f)
    d.text((8, size - 46), f"SL {sl}", fill=230, font=f)
    d.text((8, size - 26), f"ST {st}", fill=230, font=f)
    d.text((size - 90, size - 26), f"IM {index}", fill=230, font=f)
    d.text((10, size // 2), "R", fill=230, font=_font(18))
    d.text((size - 22, size // 2), "L", fill=230, font=_font(18))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()
