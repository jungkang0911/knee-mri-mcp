"""prebuild + manifest, prebuilt-only tools, list_series without OCR, hospital mask templates."""

from __future__ import annotations

import io
import json
import time

import pytest
from mcp import Client
from PIL import Image
from synth import FAKE_PII_TOKENS, knee_image
from test_tools import _ocr_all, _png

from knee_mcp import redact as R
from knee_mcp.config import Config
from knee_mcp.server import build_server
from knee_mcp.service import KneeService
from knee_mcp.templates import DEFAULT_TEMPLATES, apply_template, load_templates


@pytest.fixture
def prebuilt(tmp_path, drive):
    cfg = Config(cache_dir=tmp_path / "cache", ocr_langs="eng", workers=4, exclude_folders=["匿名化"])  # budget 0
    svc = KneeService(cfg, drive)
    msgs: list[str] = []
    manifest = svc.prebuild(progress=msgs.append)
    return cfg, svc, manifest, msgs


def test_manifest_fields_and_incremental(prebuilt, drive):
    cfg, svc, manifest, msgs = prebuilt
    data = json.loads((cfg.cache_dir / "manifest.json").read_text("utf-8"))
    assert data == manifest and len(data) == 20 + 6 + 2 + 3
    e = next(x for x in data if x["study"] == "2019-03-20_TSGH" and x["index"] == 13)
    assert set(e) == {"study", "series", "index", "plane", "sl", "st", "size", "fileId", "modifiedTime",
                      "masked_path", "redaction_status"}
    assert e["series"] == 7 and e["plane"] == "COR PD" and e["sl"] == "9.0" and e["st"] == "3.0"
    assert e["size"] == [512, 512] and e["redaction_status"] == "ok" and e["masked_path"].startswith("redacted/")
    assert (cfg.cache_dir / e["masked_path"]).exists()
    # Re-running skips everything that did not change.
    n = drive.downloads
    msgs2: list[str] = []
    svc.prebuild(progress=msgs2.append)
    assert drive.downloads == n and "0 to do" in msgs2[0]


async def test_tools_answer_fast_from_prebuilt_cache(prebuilt, drive, monkeypatch):
    cfg, svc, _, _ = prebuilt

    def no_ocr(*a, **k):
        raise AssertionError("OCR must not run at request time")

    monkeypatch.setattr(R, "ocr_words", no_ocr)
    monkeypatch.setattr(drive, "download", lambda f: (_ for _ in ()).throw(AssertionError("no download")))
    server = build_server(cfg, svc)
    async with Client(server) as c:
        t = time.monotonic()
        ls = await c.call_tool("list_series", {"study": "2024-12-03_VGHTPE"})
        assert time.monotonic() - t < 3 and "SAG PD" in ls.content[0].text and "5/5" in ls.content[0].text
        t = time.monotonic()
        r = await c.call_tool("get_slices", {"study": "2019-03-20", "series": "7", "start": 1, "end": 8})
        assert time.monotonic() - t < 3 and [x.type for x in r.content].count("image") == 8
        sl = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 4, "crop": [100, 100, 300, 300]})
        assert sl.content[0].type == "image" and _png(sl.content[0]).size == (200, 200)


async def test_not_prebuilt_answers_immediately(tmp_path, drive):
    cfg = Config(cache_dir=tmp_path / "cache", ocr_langs="eng", workers=1, exclude_folders=["匿名化"])
    svc = KneeService(cfg, drive)
    server = build_server(cfg, svc)
    async with Client(server) as c:
        t = time.monotonic()
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 2})
        ls = await c.call_tool("list_series", {"study": "2019-03-20"})
    assert time.monotonic() - t < 3
    assert r.is_error and "not prebuilt yet" in r.content[0].text
    assert "0/20" in ls.content[0].text and "not prebuilt yet" in ls.content[0].text


def test_templates_black_out_bands_and_keep_anatomy():
    img = Image.open(io.BytesIO(knee_image(5))).convert("L")
    apply_template(img, DEFAULT_TEMPLATES["TSGH"])
    w, h = img.size
    assert img.getpixel((w // 2, int(0.05 * h))) == 0 and img.getpixel((10, int(0.9 * h))) == 0
    assert img.getpixel((w // 2, 150)) > 90  # anatomy (femur) untouched
    assert load_templates('{"tsgh": [[0, 0, 1, 0.1]]}')["TSGH"] == [(0.0, 0.0, 1.0, 0.1)]


def test_template_redaction_removes_pii_and_reads_params_first():
    raw = knee_image(13)
    img, ov = R.redact(raw, "eng", [], [list(r) for r in DEFAULT_TEMPLATES["TSGH"]])
    assert ov.plane == "COR PD" and ov.sl == "-12.5" and ov.st == "3.0"  # read before the bands are blacked out
    text = _ocr_all(img)
    for token in FAKE_PII_TOKENS:
        assert token not in text


def test_center_noise_single_cjk_is_not_a_leak():
    size = (512, 512)
    word = lambda t, c, box: R.Word(t, c, box, (0, 1, 1, 1))  # noqa: E731
    # Real-image cases that used to withhold: lone or tiny "characters" in the anatomy area.
    assert not R._is_residual(word("骨", 96.0, (225, 311, 230, 313)), False, size, [])
    specks = [word("骨", 93.0, (202, 310, 215, 314)), word("肉", 96.0, (225, 311, 230, 313))]
    assert not R._center_cjk_pairs(specks, [False, False], size)
    mixed = [word("骨", 83.0, (181, 145, 186, 147)), word("肉", 80.0, (189, 141, 197, 152))]
    assert not R._center_cjk_pairs(mixed, [False, False], size)
    same_glyph_twice = [word("肉", 77.0, (189, 141, 196, 152)), word("肉", 80.0, (189, 141, 197, 152))]
    assert not R._center_cjk_pairs(same_glyph_twice, [False, False], size)
    # A name split into text-sized single characters in a row is still caught.
    name = [word("王", 88.0, (200, 300, 213, 314)), word("小", 90.0, (216, 300, 229, 314)), word("明", 85.0, (232, 300, 245, 314))]
    assert R._center_cjk_pairs(name, [False] * 3, size)
    assert R._is_residual(word("王小明", 80.0, (200, 300, 245, 314)), False, size, [])
    assert not R._is_residual(word("王小", 70.0, (200, 300, 230, 314)), False, size, [])  # below 75 in the center
    assert R._is_residual(word("王", 60.0, (2, 2, 16, 17)), False, size, [])  # edges stay strict
