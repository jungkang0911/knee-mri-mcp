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
                      "masked_path", "redaction_status", "mask_boxes", "review_boxes"}
    assert e["series"] == 7 and e["plane"] == "COR PD" and e["sl"] == "9" and e["st"] == "3.0"
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
    assert img.getpixel((w // 2, int(0.05 * h))) == 0 and img.getpixel((10, int(0.98 * h))) == 0
    assert img.getpixel((w // 2, 150)) > 90  # anatomy (femur) untouched
    assert load_templates('{"tsgh": [[0, 0, 1, 0.1]]}')["TSGH"] == [(0.0, 0.0, 1.0, 0.1)]


def test_template_redaction_removes_pii_and_reads_params_first():
    raw = knee_image(13)
    r = R.redact(raw, "eng", [], [list(x) for x in DEFAULT_TEMPLATES["TSGH"]])
    assert r.overlay.plane == "COR PD" and r.overlay.sl == "-12.5" and r.overlay.st == "3.0"
    text = _ocr_all(r.img)
    for token in FAKE_PII_TOKENS:
        assert token not in text
    assert r.mask_boxes and not r.review_boxes


def test_pii_content_rules():
    pii = R.looks_like_pii
    for t in ("王小明", "20481736", "1975/04/12", "19750412", "12-APR-1975", "A123456789", "1234-5678",
              "29-July-1983", "31-December-2018", "8-March-2023"):
        assert pii(t), t
    for t in ("R", "L", "A", "P", "S", "I", "SL", "-108.21", "4.00", "COR", "PD", "FS", "512", "TR", "3000", "骨"):
        assert not pii(t), t
    assert pii("COR", ["cor"])  # configured terms always count
    size = (512, 512)
    assert R.in_edge((5, 5, 60, 20), size) and R.in_edge((10, 480, 80, 500), size)
    assert not R.in_edge((200, 200, 260, 220), size)


def _draw_text(img, xy, text, size=16, backdrop=False):
    from PIL import ImageDraw
    from synth import _font

    d = ImageDraw.Draw(img)
    if backdrop:  # overlay-style text on a dark patch, so OCR reads it reliably inside the "anatomy"
        x, y = xy
        d.rectangle((x - 4, y - 3, x + 9 * len(text) + 6, y + size + 5), fill=0)
    d.text(xy, text, fill=250, font=_font(size))


@pytest.mark.parametrize("index", [3, 13])
def test_anatomy_is_never_masked(index):
    """Outside the edge area the masked image equals the original pixel for pixel."""
    from PIL import ImageChops

    raw = knee_image(index)
    tpl = [list(x) for x in DEFAULT_TEMPLATES["TSGH"]]
    r = R.redact(raw, "eng", [], tpl)
    reference = Image.open(io.BytesIO(raw)).convert(r.img.mode)
    diff = ImageChops.difference(r.img.convert("L"), reference.convert("L"))
    w, h = r.img.size
    changed_inside = sum(
        1 for y in range(0, h, 2) for x in range(0, w, 2)
        if diff.getpixel((x, y)) > 0 and not R.in_edge((x, y, x + 1, y + 1), (w, h))
    )
    assert changed_inside == 0
    assert all(R.in_edge(b, (w, h)) for b in r.mask_boxes)


def test_parameters_and_orientation_stay_visible():
    raw = knee_image(13)
    r = R.redact(raw, "eng", [], [list(x) for x in DEFAULT_TEMPLATES["TSGH"]])
    text = _ocr_all(r.img)
    assert "COR" in text and "SL" in text and "-12.5" in text  # lower-left parameters are not personal data


def test_pii_inside_anatomy_goes_to_review_not_mask():
    img = Image.open(io.BytesIO(knee_image(7, pii=False))).convert("L")
    _draw_text(img, (150, 250), "MRN 20481736", backdrop=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    r = R.redact(buf.getvalue(), "eng", [], [list(x) for x in DEFAULT_TEMPLATES["TSGH"]])
    assert r.review_boxes and all(not R.in_edge(b, r.img.size) for b in r.review_boxes)
    assert all(R.in_edge(b, r.img.size) for b in r.mask_boxes)  # nothing blacked out in the anatomy
    assert "20481736" in _ocr_all(r.img)  # left visible for the owner to judge


def test_pii_text_in_edge_outside_template_is_masked_tightly():
    img = Image.open(io.BytesIO(knee_image(7, pii=False))).convert("L")
    _draw_text(img, (8, 380), "1975/04/12")  # lower-left edge, below the template bands
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    r = R.redact(buf.getvalue(), "eng", [], [list(x) for x in DEFAULT_TEMPLATES["TSGH"]])
    assert "1975" not in _ocr_all(r.img)
    extra = [b for b in r.mask_boxes if b[1] > 0.19 * 512 and b[3] < 0.955 * 512]
    assert extra and all((b[2] - b[0]) < 200 and (b[3] - b[1]) < 40 for b in extra)  # tight boxes only


async def test_review_flow_manifest_caption_and_debug(tmp_path, drive):
    img = Image.open(io.BytesIO(knee_image(5))).convert("RGB")
    _draw_text(img, (150, 250), "MRN 20481736", backdrop=True)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    fid = next(k for k, (f, _) in drive.files.items() if f.name.endswith("S7_I0005.jpg"))
    drive.files[fid] = (drive.files[fid][0], buf.getvalue())
    cfg = Config(cache_dir=tmp_path / "cache", ocr_langs="eng", workers=4, exclude_folders=["匿名化"])
    svc = KneeService(cfg, drive)
    debug = tmp_path / "debug"
    manifest = svc.prebuild(debug_dir=debug)
    e5 = next(e for e in manifest if e["study"] == "2019-03-20_TSGH" and e["index"] == 5)
    e6 = next(e for e in manifest if e["study"] == "2019-03-20_TSGH" and e["index"] == 6)
    assert e5["redaction_status"] == "review" and e5["review_boxes"] and e5["mask_boxes"]
    assert e6["redaction_status"] == "ok" and e6["mask_boxes"] and e6["review_boxes"] == []
    assert (debug / "2019-03-20_TSGH_S7_I0005.png").exists() and (debug / "2019-03-20_TSGH_S7_I0006.png").exists()
    server = build_server(cfg, svc)
    async with Client(server) as c:
        r5 = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 5})
        r6 = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 6})
        assert r5.is_error and "pending manual review" in r5.content[0].text
        assert not r6.is_error and "masked boxes 2" in r6.content[1].text
        f5 = svc.catalog.index().get_study("2019-03-20").series[7].get(5).file
        svc.proc.decide_review(f5, "mask")
        r5b = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 5})
    assert not r5b.is_error and r5b.content[0].type == "image"
    assert "20481736" not in _ocr_all(_png(r5b.content[0]))


def test_series_vote_and_sl_repair():
    from knee_mcp.service import repair_sl, vote_plane

    assert vote_plane(["SAG PD"] * 10 + ["SAG PD FS"] * 6) == "SAG PD FS"
    assert vote_plane(["COR PD", "COR PD", "COR T1"]) == "COR PD"
    fixed = repair_sl({1: "84.23", 2: "8023", 3: "76.23", 4: "72.2", 6: "64.23", 7: "6023"})
    assert fixed[2] == "80.23" and fixed[7] == "60.23" and fixed[6] == "64.23"
    assert repair_sl({1: "84.23", 2: "80.23", 3: "76.23", 4: ""})[4] == "~72.2"  # unread: from the line
    # Real 2024 S4 readings: 39.91, 36.61, ..., one misread as "3"
    s4 = {16: "39.91", 17: "36.61", 18: "33.31", 19: "3", 20: "26.71", 21: "23.41", 22: "20.11"}
    out = repair_sl(s4)
    assert out[19] == "~30" and out[16] == "39.91" and out[22] == "20.11"
    # Axial positions above 400 mm are real, not misreads.
    ax = repair_sl({i: f"{424.3 - 4 * i:.2f}" for i in range(1, 8)})
    assert ax[1] == "420.3" and not any(v.startswith("~") for v in ax.values())


def test_plane_from_folder_names():
    from knee_mcp.service import plane_from_folders

    assert plane_from_folders(("2024-12-03_右膝MRI", "冠狀面_T2脂肪抑制FSE")) == "COR T2 FS FSE"
    assert plane_from_folders(("x", "矢狀面_PD+T2 FSE")) == "SAG PD T2 FSE"
    assert plane_from_folders(("x", "右膝_橫切面_T2脂肪抑制FSE")) == "Axial T2 FS FSE"
    assert plane_from_folders(("x", "冠狀面_3D MERGE")) == "COR 3D MERGE"
    assert plane_from_folders(("x", "序列-010")) == ""


def test_cjk_name_split_into_single_characters_is_pii():
    w = lambda t, x: R.Word(t, 90.0, (x, 10, x + 13, 24), (0, 1, 1, 1))  # noqa: E731
    words = [w("王", 10), w("小", 26), w("明", 42), w("R", 300)]
    hits = R.pii_words(words)
    assert {h.text for h in hits} == {"王", "小", "明"}
    assert R.pii_words([w("骨", 200)]) == []  # a lone character is not a name


def test_studies_without_hospital_code_get_the_xray_band():
    from knee_mcp.templates import DEFAULT_TEMPLATES

    assert DEFAULT_TEMPLATES[""][0] == (0.0, 0.0, 1.0, 0.13)


def test_unconfirmed_folder_becomes_its_own_study():
    from knee_mcp.catalog import build_index
    from knee_mcp.drive import DriveFile

    anon = "日期待確認_匿名化MRI（原標記20251126）"
    files = [DriveFile(f"a{s}{i}", f"image-{i:05d}.jpg", (anon, f"序列-{s:05d}"), "t") for s in range(7) for i in range(2)]
    files += [
        DriveFile("k1", "SAG_T2_series-00000_posterior_effusion_fabella_image-00010.jpg", (anon, "重點影像"), "t"),
        DriveFile("k2", "AX_T1_series-00001_fabella_bone_confirm_image-00010.jpg", (anon, "重點影像"), "t"),
        DriveFile("c1", "01_2019術前_冠狀面_盤狀外側半月板.png", (anon, "Claude標註圖"), "t"),
        DriveFile("d1", "2019_S10_I10.jpg", ("2026-09-27_膝部門診影像討論包", "images"), "t"),
    ]
    idx = build_index(files, {}, [], ["門診影像討論包", "Claude標註圖"], None, {"匿名化MRI": "2025-11-26_unconfirmed"})
    assert [st.id for st in idx.studies] == ["2025-11-26_unconfirmed"]
    st = idx.studies[0]
    assert st.kind == "MRI" and sorted(st.series) == [0, 1, 2, 3, 4, 5, 6, 99]
    assert [s.index for s in st.series[99].slices] == [1, 2]  # key images renumbered, same image-00010 no clash
    assert "excluded 2 files" in idx.issues[-1] and len(idx.issues) == 1


async def test_masking_off_serves_unmasked_quickly(tmp_path, drive):
    cfg = Config(cache_dir=tmp_path / "cache", ocr_langs="eng", workers=2, exclude_folders=["匿名化"], masking=False)
    svc = KneeService(cfg, drive)
    svc.prebuild()
    server = build_server(cfg, svc)
    async with Client(server) as c:
        ls = await c.call_tool("list_studies", {})
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "7", "index": 13})
    assert "masking is DISABLED" in ls.content[0].text
    assert r.content[0].type == "image" and "masked boxes 0" in r.content[1].text and "COR PD" in r.content[1].text
    assert "XIAOMING" in _ocr_all(_png(r.content[0]))  # nothing masked, by design


def test_duplicate_folder_is_excluded_and_explained():
    from knee_mcp.catalog import build_index
    from knee_mcp.drive import DriveFile

    anon = "日期待確認_匿名化MRI（原標記20251126）"
    files = [DriveFile(f"a{i}", f"image-{i:05d}.jpg", (anon, "序列-00003"), "t") for i in range(3)]
    files.append(DriveFile("o1", "2019-03-20_TSGH_RK_S7_I0001.jpg", ("2019-03-20_TSGH_MRI",), "t"))
    idx = build_index(files, {}, [], [], None, None, {"匿名化MRI": "2019-03-20_TSGH"})
    assert [st.id for st in idx.studies] == ["2019-03-20_TSGH"]
    assert idx.issues == ["excluded 3 files that are a duplicate copy of study 2019-03-20_TSGH "
                          "(KNEE_DUPLICATE_FOLDERS); they are not a separate exam"]
