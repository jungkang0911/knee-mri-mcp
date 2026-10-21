"""Contrast-enhanced (+C) series stay recognisable in every listing and caption."""

from __future__ import annotations

from mcp import Client

from knee_mcp import redact as R
from knee_mcp.service import contrast_label, has_contrast, plane_display


def test_overlay_ocr_keeps_contrast_markers():
    w = lambda t, x, line=(0, 1, 1, 1): R.Word(t, 90.0, (x, 480, x + 30, 494), line)  # noqa: E731
    assert R.read_overlay([w("SAG", 0), w("T1", 40), w("+C", 80), w("FS", 120)]).plane == "SAG T1 FS +C"
    assert R.read_overlay([w("COR_T1_FS_C+", 0)]).plane == "COR T1 FS +C"
    assert R.read_overlay([w("AX", 0), w("T1", 40), w("post", 80), w("Gd", 120)]).plane == "Axial T1 +C"
    assert R.read_overlay([w("SAG", 0), w("PD", 40), w("FS", 80)]).plane == "SAG PD FS"
    assert has_contrast("SAG FSE T1 +C FS") and not has_contrast("SAG PD FS")
    assert contrast_label("SAG FSE T1 +C FS") == "有" and contrast_label("COR PD") == "無" and contrast_label("") == "?"
    assert plane_display("SAG FSE T1 +C FS") == "SAG FSE T1 +C FS（顯影後）" and plane_display("COR PD") == "COR PD"


async def test_contrast_shown_in_listings_and_captions(server, service):
    service.prebuild()
    async with Client(server) as c:
        studies = await c.call_tool("list_studies", {})
        series = await c.call_tool("list_series", {"study": "2022-05-19_VGHTPE"})
        s12 = await c.call_tool("get_slice", {"study": "2022-05-19_VGHTPE", "series": "S12", "index": 1})
        s4 = await c.call_tool("get_slices", {"study": "2022-05-19_VGHTPE", "series": "S4", "start": 1, "end": 2})
        cmp = await c.call_tool("compare", {"a": {"study": "2022-05-19_VGHTPE", "series": 4, "index": 1},
                                            "b": {"study": "2022-05-19_VGHTPE", "series": 12, "index": 1}})
    rows = {ln.split(" | ")[0]: ln for ln in series.content[0].text.splitlines()}
    assert "| SAG FSE T1 +C FS | 有 |" in rows["S12"] and "| Axial T2 FSE FS | 無 |" in rows["S4"]
    assert "contrast" in series.content[0].text.splitlines()[1]
    st_row = next(ln for ln in studies.content[0].text.splitlines() if ln.startswith("2022-05-19_VGHTPE"))
    assert st_row.endswith("| 1/2 (S12)")
    assert next(ln for ln in studies.content[0].text.splitlines() if ln.startswith("2019-03-20_TSGH")).endswith("| -")
    assert "右膝 SAG FSE T1 +C FS（顯影後）" in s12.content[1].text
    assert all("顯影後" not in x.text for x in s4.content if x.type == "text")
    assert "S12 I0001 SAG FSE T1 +C FS（顯影後）" in cmp.content[1].text
