"""overrides.json "info": get_study_info, list_studies marker, paired-series compare."""

from __future__ import annotations

import json

from mcp import Client

from knee_mcp.config import Config
from knee_mcp.server import build_server
from knee_mcp.service import KneeService

INFO = {"studies": {"2022-05-19_VGHTPE": {
    "info": {"scanner": "Hospital B 1.5T", "contrast": "yes, IV (S12 post-contrast)", "report_date": "2022-06-02",
             "report": ["Minimal effusion; medial plica", "Tiny lateral meniscus free-edge defect"],
             "pairs": [["S12", "S4", "same slice positions"]], "notes": ["between arthroscopies"]},
    "series": {"4": {"plane": "Axial T2 FSE FS", "st": "4", "phase": "pre-contrast"},
               "12": {"plane": "SAG T1 FS +C", "st": "4", "phase": "post-contrast", "note": "matches S4"}},
}}}


def _server(tmp_path, drive):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(INFO, ensure_ascii=False), encoding="utf-8")
    cfg = Config(cache_dir=tmp_path / "cache", ocr_langs="eng", workers=2, exclude_folders=["匿名化"],
                 tool_budget=600, overrides_path=path)
    svc = KneeService(cfg, drive)
    return build_server(cfg, svc), svc


async def test_get_study_info_and_list_studies_marker(tmp_path, drive):
    server, _ = _server(tmp_path, drive)
    async with Client(server) as c:
        info = await c.call_tool("get_study_info", {"study": "2022-05-19"})
        none = await c.call_tool("get_study_info", {"study": "2019-03-20"})
        studies = await c.call_tool("list_studies", {})
    t = info.content[0].text
    assert t.startswith("2022-05-19_VGHTPE: 2022-05-19, VGHTPE, 右膝, MRI, 2 series, 4 images")
    assert "scanner: Hospital B 1.5T" in t and "contrast: yes, IV" in t
    assert "radiology report (2022-06-02):" in t and "1. Minimal effusion; medial plica" in t
    assert "- S12 <-> S4: same slice positions" in t and "S12: post-contrast, matches S4" in t
    assert "note: between arthroscopies" in t
    assert "| SAG T1 FS +C | 有 |" in t  # series table included
    assert "no extra information" in none.content[0].text
    rows = {ln.split(" | ")[0]: ln for ln in studies.content[0].text.splitlines()}
    assert rows["2022-05-19_VGHTPE"].endswith("| report 2022-06-02") and rows["2019-03-20_TSGH"].endswith("| -")
    assert "get_study_info" in studies.content[0].text


async def test_compare_paired_series_without_second_index(tmp_path, drive):
    server, _ = _server(tmp_path, drive)
    async with Client(server) as c:
        ok = await c.call_tool("compare", {"a": {"study": "2022-05-19", "series": 4, "index": 2},
                                           "b": {"study": "2022-05-19", "series": 12}})
        bad = await c.call_tool("compare", {"a": {"study": "2022-05-19", "series": 4, "index": 2},
                                            "b": {"study": "2019-03-20", "series": 7}})
    assert not ok.is_error and ok.content[0].type == "image"
    assert "S12 I0002" in ok.content[1].text and "paired series: same slice positions" in ok.content[1].text
    assert bad.is_error and "paired" in bad.content[0].text
