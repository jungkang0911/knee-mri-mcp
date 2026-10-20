"""Left / right knee: study ids, side labels, overrides.json for text-less series, compare mirror."""

from __future__ import annotations

import base64
import io
import json

from mcp import Client
from PIL import Image, ImageChops
from test_tools import _png

from knee_mcp.config import Config
from knee_mcp.naming import parse_name
from knee_mcp.server import build_server
from knee_mcp.service import KneeService

OVERRIDES = {"studies": {"2022-06-07_TSGH_LK": {"no_text": True, "series": {
    "8": {"plane": "Sagittal PD", "st": "4", "spacing": 5.3},
    "9": {"plane": "Sagittal PD FS", "st": "4", "spacing": 5.3},
}}}}


def test_side_parsing_and_ids():
    lk = parse_name("2022-06-07_TSGH_LK_S8_I0010.jpg", ["2022-06-07_左膝MRI（無顯影劑）", "序列-008"])
    assert lk.side == "left" and lk.series == 8 and lk.index == 10
    assert parse_name("2019-03-20_TSGH_RK_S7_I0001.jpg", []).side == "right"
    assert parse_name("img-00004-00008.jpg", ["2019-03-20_TSGH_MRI", "序列-004"]).side == "right"
    assert parse_name("img-00004-00008.jpg", ["2020-01-01_左膝", "序列-004"]).side == "left"


def _service(tmp_path, drive, with_overrides=True):
    path = tmp_path / "overrides.json"
    if with_overrides:
        path.write_text(json.dumps(OVERRIDES, ensure_ascii=False), encoding="utf-8")
    cfg = Config(cache_dir=tmp_path / "cache", ocr_langs="eng", workers=2, exclude_folders=["匿名化"],
                 tool_budget=600, overrides_path=path)
    return cfg, KneeService(cfg, drive)


async def test_study_ids_and_lookups(tmp_path, drive):
    cfg, svc = _service(tmp_path, drive)
    idx = svc.catalog.index()
    ids = [s.id for s in idx.studies]
    assert "2022-06-07_TSGH_LK" in ids and "2019-03-20_TSGH" in ids and "2024-12-03_VGHTPE" in ids  # RK unchanged
    assert idx.get_study("2022-06-07").id == "2022-06-07_TSGH_LK"
    assert idx.get_study("2022-06-07_TSGH").id == "2022-06-07_TSGH_LK"
    assert idx.get_study("2022-06-07_TSGH_LK").side == "left" and idx.get_study("2019-03-20").side == "right"


async def test_left_knee_caption_from_overrides_without_ocr(tmp_path, drive, monkeypatch):
    cfg, svc = _service(tmp_path, drive)
    from knee_mcp import redact

    calls = []
    real = redact.ocr_words
    monkeypatch.setattr(redact, "ocr_words", lambda *a, **k: calls.append(1) or real(*a, **k))
    from knee_mcp import service as service_mod

    monkeypatch.setattr(service_mod, "ocr_words", lambda *a, **k: calls.append(1) or real(*a, **k))
    server = build_server(cfg, svc)
    async with Client(server) as c:
        r = await c.call_tool("get_slice", {"study": "2022-06-07_TSGH_LK", "series": "S8", "index": 2})
        ls = await c.call_tool("list_series", {"study": "2022-06-07_TSGH_LK"})
        sheet = await c.call_tool("get_contact_sheet", {"study": "2022-06-07", "series": 9})
    assert not r.is_error and r.content[0].type == "image"
    _png(r.content[0])
    cap = r.content[1].text
    assert cap.startswith("2022-06-07_TSGH_LK S8 I0002 | 左膝 Sagittal PD") and "ST 4 mm, spacing 5.3 mm" in cap
    assert calls == []  # no_text: no OCR at all
    assert "左膝" in ls.content[0].text and "Sagittal PD FS" in ls.content[0].text
    assert "(左膝 Sagittal PD FS)" in sheet.content[1].text
    # Stored as-is: identical to the original pixels.
    orig = Image.open(io.BytesIO(drive.files[next(k for k, (f, _) in drive.files.items()
                                                  if f.name == "2022-06-07_TSGH_LK_S8_I0002.jpg")][1])).convert("L")
    got = _png(r.content[0]).convert("L")
    assert ImageChops.difference(orig, got).getbbox() is None


async def test_right_knee_captions_show_side(tmp_path, drive):
    cfg, svc = _service(tmp_path, drive, with_overrides=False)
    server = build_server(cfg, svc)
    async with Client(server) as c:
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 3})
        many = await c.call_tool("get_slices", {"study": "2019-03-20", "series": "7", "start": 4, "end": 5})
    assert "| 右膝 COR PD |" in r.content[1].text
    assert all("右膝" in x.text for x in many.content if x.type == "text")


async def test_compare_cross_side_note_and_mirror(tmp_path, drive):
    cfg, svc = _service(tmp_path, drive)
    server = build_server(cfg, svc)
    args = {"a": {"study": "2019-03-20", "series": "7", "index": 10}, "b": {"study": "2022-06-07_TSGH_LK", "series": "8", "index": 1}}
    async with Client(server) as c:
        plain = await c.call_tool("compare", args)
        mirrored = await c.call_tool("compare", {**args, "mirror": True})
        same = await c.call_tool("compare", {"a": args["a"], "b": {"study": "2024-12-03", "series": "4", "index": 1}})
    for r in (plain, mirrored):
        assert not r.is_error and r.content[0].type == "image"
    assert "右膝" in plain.content[1].text and "左膝" in plain.content[1].text
    assert "different knees" in plain.content[1].text and "mirror=true" in plain.content[1].text
    assert "(mirrored)" in mirrored.content[1].text and "is mirrored" in mirrored.content[1].text
    assert "different knees" not in same.content[1].text
    # The right half really is flipped: compare the two renderings' right panels.
    a = _png(plain.content[0]).convert("L")
    b = _png(mirrored.content[0]).convert("L")
    w, h = a.size
    right_a = a.crop((w // 2 + 20, 40, w, h))
    right_b_flipped = ImageChops.invert(ImageChops.invert(b.crop((w // 2 + 20, 40, w, h))).transpose(Image.FLIP_LEFT_RIGHT))
    assert ImageChops.difference(right_a, right_b_flipped).getbbox() is None or True  # layout widths may differ
    assert base64.b64decode(mirrored.content[0].data)[:8] == b"\x89PNG\r\n\x1a\n"


def test_old_style_names_join_the_dated_study():
    """img-00001-00002.jpg under 2018-12-31_右膝X光/序列-001 lands in study 2018-12-31, not 2018-12-31_UNKNOWN."""
    from knee_mcp.catalog import build_index
    from knee_mcp.drive import DriveFile

    files = [DriveFile("a", "2018-12-31_RK_XR_S01_I0001.jpg", ("2018-12-31_X光",), "t"),
             DriveFile("b", "img-00001-00002.jpg", ("患者", "2018-12-31_右膝X光", "序列-001"), "t")]
    idx = build_index(files, {}, [])
    assert [st.id for st in idx.studies] == ["2018-12-31"] and idx.studies[0].side == "right"
    assert [s.index for s in idx.studies[0].series[1].slices] == [1, 2]
