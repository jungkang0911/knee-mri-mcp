"""Acceptance checks that can run without real Drive credentials (mock Drive + synthetic images)."""

from __future__ import annotations

import base64
import io

import pytesseract
import pytest
from mcp import Client
from pathlib import Path

from PIL import Image, ImageOps
from synth import FAKE_PII_TOKENS

from knee_mcp import imaging, redact
from knee_mcp.naming import normalize_series, parse_name


def _png(content) -> Image.Image:
    assert content.type == "image"
    assert content.mime_type == "image/png"
    raw = base64.b64decode(content.data)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(raw) <= 1_000_000
    return Image.open(io.BytesIO(raw))


def _no_base64_text(result) -> None:
    for c in result.content:
        if c.type == "text":
            assert len(c.text) < 2000 and "iVBOR" not in c.text


def _ocr_all(img: Image.Image) -> str:
    gray = img.convert("L")
    big = gray.resize((gray.width * 3, gray.height * 3))
    from PIL import ImageOps

    return " ".join(
        pytesseract.image_to_string(im, config=f"--psm {psm}")
        for im in (big, ImageOps.invert(big))
        for psm in (6, 11)
    ).upper()


# ---- acceptance 1: get_slice returns ImageContent, not text -------------------------------------

async def test_get_slice_returns_image_content(server):
    async with Client(server) as c:
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "S7", "index": 13})
    assert not r.is_error, r.content
    assert [x.type for x in r.content] == ["image", "text"]
    _png(r.content[0])
    _no_base64_text(r)
    caption = r.content[1].text
    assert "2019-03-20_TSGH S7 I0013" in caption
    assert "COR PD" in caption and "SL 9.0" in caption and "ST 3.0" in caption


async def test_crop_and_zoom(server):
    async with Client(server) as c:
        r = await c.call_tool("get_slice", {"study": "2019-03-20_TSGH", "series": 7, "index": 2,
                                            "crop": [100, 50, 300, 250], "zoom": 2})
    img = _png(r.content[0])
    assert img.size == (400, 400)


async def test_get_slices_max_8(server):
    async with Client(server) as c:
        ok = await c.call_tool("get_slices", {"study": "2019-03-20", "series": "7", "start": 3, "end": 10})
        too_many = await c.call_tool("get_slices", {"study": "2019-03-20", "series": "7", "start": 1, "end": 9})
    assert [x.type for x in ok.content].count("image") == 8
    for x in ok.content:
        if x.type == "image":
            _png(x)
    assert too_many.is_error and "at most 8" in too_many.content[0].text


async def test_compare_side_by_side(server):
    async with Client(server) as c:
        r = await c.call_tool("compare", {
            "a": {"study": "2019-03-20", "series": "S7", "index": 10},
            "b": {"study": "2024-12-03", "series": "S04", "index": 2, "crop": [0, 0, 400, 400]},
        })
    assert not r.is_error, r.content
    img = _png(r.content[0])
    assert img.width > img.height
    assert "2019-03-20 TSGH S7 I0010" in r.content[1].text
    assert "2024-12-03 VGHTPE S4 I0002" in r.content[1].text


# ---- acceptance 3: no PII survives -------------------------------------------------------------

@pytest.mark.parametrize("index", [1, 13, 20])
async def test_returned_2019_image_has_no_pii(server, index):
    async with Client(server) as c:
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": index})
    text = _ocr_all(_png(r.content[0]))
    for token in FAKE_PII_TOKENS:
        assert token not in text, f"{token!r} survived redaction"
    # Orientation letters and technical parameters stay readable.
    assert "COR" in text and "SL" in text and "ST" in text


async def test_contact_sheet_has_no_pii(server):
    async with Client(server) as c:
        r = await c.call_tool("get_contact_sheet", {"study": "2019-03-20", "series": "7", "cols": 4})
    text = _ocr_all(_png(r.content[0]))
    for token in FAKE_PII_TOKENS:
        assert token not in text


async def test_image_withheld_when_masking_fails(server, monkeypatch):
    monkeypatch.setattr(redact, "_mask", lambda img, words, flags: 0)  # simulate a masking miss
    async with Client(server) as c:
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 5})
    assert r.is_error
    assert all(x.type == "text" for x in r.content)
    assert "withheld" in r.content[0].text
    for token in FAKE_PII_TOKENS:
        assert token not in r.content[0].text.upper()


def test_pii_terms_checked_after_masking():
    from synth import knee_image

    from knee_mcp.errors import RedactionFailed

    # A configured term that gets masked passes; one that would stay visible (here the allowlisted
    # sequence word "COR", standing in for a real name the allowlist missed) must withhold the image.
    redact.redact(knee_image(3), "eng", ["XIAOMING"])
    with pytest.raises(RedactionFailed):
        redact.redact(knee_image(3), "eng", ["COR"])


# ---- acceptance 4: S4 == S04 ------------------------------------------------------------------

def test_series_normalization():
    assert normalize_series("S4") == normalize_series("S04") == normalize_series("4") == normalize_series(4) == 4
    a = parse_name("2024-12-03_VGHTPE_RK_S4_I0001.jpg", [])
    b = parse_name("2024-12-03_VGHTPE_RK_S04_I0004.jpg", [])
    assert a.series == b.series == 4 and a.strict and b.strict


async def test_s4_and_s04_are_one_series(server):
    async with Client(server) as c:
        r = await c.call_tool("list_series", {"study": "2024-12-03"})
        s1 = await c.call_tool("get_slice", {"study": "2024-12-03", "series": "S4", "index": 5})
        s2 = await c.call_tool("get_slice", {"study": "2024-12-03", "series": "S04", "index": 1})
    text = r.content[0].text
    rows = [ln for ln in text.splitlines() if ln.startswith("S4 ")]
    assert len(rows) == 1 and "| 5 |" in rows[0] and "I0001-I0005" in rows[0]
    assert "S04" not in text
    assert not s1.is_error and not s2.is_error


# ---- acceptance 2 (mock data): contact sheet order and count ------------------------------------

async def test_contact_sheet_order(server, monkeypatch):
    seen = {}
    real = imaging.contact_sheet

    def spy(tiles, cols, thumb=180):
        seen["labels"] = [label for label, _ in tiles]
        seen["withheld"] = sum(img is None for _, img in tiles)
        return real(tiles, cols, thumb)

    monkeypatch.setattr(imaging, "contact_sheet", spy)
    async with Client(server) as c:
        r = await c.call_tool("get_contact_sheet", {"study": "2019-03-20", "series": "S07"})
    assert not r.is_error, r.content
    assert seen["labels"] == [f"I{i:04d}" for i in range(1, 21)]
    assert seen["withheld"] == 0
    assert "20 images" in r.content[1].text
    img = _png(r.content[0])
    assert img.width > 5 * 180  # 5 columns by default
    assert img.height > 4 * 180  # 4 rows for 20 tiles


# ---- listing, naming issues, cache, safe errors -----------------------------------------------

async def test_list_studies_and_issues(server):
    async with Client(server) as c:
        r = await c.call_tool("list_studies", {})
    text = r.content[0].text
    assert "2018-12-31_TSGH | 2018-12-31 | TSGH | X-ray | 1 | 2" in text
    assert "2019-03-20_TSGH | 2019-03-20 | TSGH | MRI | 1 | 20" in text
    assert "2024-12-03_VGHTPE | 2024-12-03 | VGHTPE | MRI | 2 | 6" in text
    assert "img-00001-00001.jpg: name does not follow the naming rule" in text


async def test_list_series_reads_overlay(server):
    async with Client(server) as c:
        r = await c.call_tool("list_series", {"study": "2019-03-20"})
    row = [ln for ln in r.content[0].text.splitlines() if ln.startswith("S7 ")][0]
    assert "| 20 |" in row and "COR PD" in row and "-27 .. 30" in row


async def test_drive_cache_by_file_id_and_mtime(server, drive):
    async with Client(server) as c:
        await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 1})
        n = drive.downloads
        await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 1, "zoom": 2})
        assert drive.downloads == n  # served from cache
        # A new modifiedTime (file replaced in Drive) invalidates the cache.
        fid = next(k for k, (f, _) in drive.files.items() if f.name.endswith("S7_I0001.jpg"))
        f, data = drive.files[fid]
        drive.files[fid] = (type(f)(f.id, f.name, f.folders, "2026-01-01T00:00:00.000Z"), data)
        await c.call_tool("list_studies", {})  # refreshes the listing
        await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 1})
        assert drive.downloads == n + 1


async def test_errors_do_not_leak_paths(server, drive, monkeypatch):
    def boom(file):
        raise OSError(r"C:\secret\cache\drive\f0001_2019-03-20_TSGH_RK_S7_I0001.jpg WANG XIAOMING")

    monkeypatch.setattr(drive, "download", boom)
    async with Client(server) as c:
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 1})
        missing = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 999})
    assert r.is_error
    msg = r.content[0].text
    assert "internal error" in msg and "\\" not in msg and "WANG" not in msg and "secret" not in msg
    assert missing.is_error and "I0999 not found" in missing.content[0].text


def test_control_ocr_sees_pii_on_unredacted_image():
    """Guards the no-PII tests above against passing vacuously (OCR must find the fake PII pre-redaction)."""
    from synth import knee_image

    text = _ocr_all(Image.open(io.BytesIO(knee_image(13))))
    for token in FAKE_PII_TOKENS:
        assert token in text


@pytest.mark.skipif(
    "chi_tra" not in pytesseract.get_languages() or not Path(r"C:\Windows\Fonts\msjh.ttc").exists(),
    reason="needs tesseract chi_tra and the Microsoft JhengHei font",
)
def test_chinese_name_is_masked():
    from synth import knee_image

    raw = knee_image(4, cjk_name="王小明 病歷號 20481736")
    gray = Image.open(io.BytesIO(raw)).convert("L")
    before = pytesseract.image_to_string(ImageOps.invert(gray.resize((1536, 1536))), lang="chi_tra+eng", config="--psm 11")
    assert "王" in before or "明" in before  # control: OCR can read the name on the original
    img, _ = redact.redact(raw, "eng+chi_tra", [])
    big = ImageOps.invert(img.convert("L").resize((1536, 1536)))
    after = pytesseract.image_to_string(big, lang="chi_tra+eng", config="--psm 11")
    assert not any(ch in after for ch in "王小明病歷號") and "20481736" not in after


async def test_unmasked_originals_never_written_to_disk(server, cfg):
    async with Client(server) as c:
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 3})
    assert not r.is_error
    raw_dir = cfg.cache_dir / "drive"
    assert not raw_dir.exists() or not any(raw_dir.iterdir())
    leftovers = [p for p in cfg.cache_dir.rglob("*") if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".bin")]
    assert leftovers == []
