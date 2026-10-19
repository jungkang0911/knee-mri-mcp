from __future__ import annotations
import re
from typing import Any
from .common import finite_float

FILENAME = re.compile(r"img-(\d+)-(\d+)\.(?:jpg|jpeg|png)$", re.I)
DIRECTIONS = set("SIAPRL")

def direction_label(word: str) -> bool:
    word=word.upper()
    return (1<=len(word)<=3 and all(c in DIRECTIONS for c in word) and len(set(word))==len(word)
            and not any(set(pair).issubset(word) for pair in ("SI","AP","RL")))

def technical_description(text: str) -> dict[str, Any]:
    """Retain only a technical vocabulary, never free OCR/DICOM descriptions."""
    t = text.upper().replace("_", " ")
    # Accept compact T2FS, but don't turn arbitrary words containing PD/T1 into metadata.
    plane = None
    for pattern, name in [(r"\b(AXIAL|AX|TRA|TRANSVERSE)\b", "axial"),
                          (r"\b(CORONAL|COR)\b", "coronal"),
                          (r"\b(SAGITTAL|SAG)\b", "sagittal")]:
        if re.search(pattern, t):
            plane = name
            break
    weights = list(dict.fromkeys(re.findall(r"(?<![A-Z0-9])(T1|T2|PD)(?=FS\b|[^A-Z0-9]|$)", t)))
    weighting = weights[0] if len(weights) == 1 else None
    fat_sat = bool(re.search(r"\bFS\b|T[12]FS\b|PDFS\b|FAT\s*(SAT|SUPPRESS)|SPAIR|STIR", t))
    technical = re.findall(r"\b(?:3D|2D|MERGE|FSE|TSE|SPAIR|STIR|DIXON)\b", t)
    desc = "_".join(([{"axial":"AX","coronal":"COR","sagittal":"SAG"}[plane]] if plane else [])
                    + weights + (["FS"] if fat_sat else []) + list(dict.fromkeys(technical)))
    return {"plane": plane, "weighting": weighting,
            "fat_sat": fat_sat if desc else None, "series_desc": desc or None,
            "mixed_weighting": len(weights) > 1}

def parse_jpeg_metadata(tokens: list[dict], name: str, expected_date: str | None) -> tuple[dict, list[dict]]:
    """OCR text exists only in process memory; conflicts are reported without raw strings."""
    issues: list[dict] = []
    rows: dict[tuple, list[dict]] = {}
    for t in tokens:
        rows.setdefault(tuple(t["line"]), []).append(t)
    lines = [" ".join(t["text"] for t in sorted(row, key=lambda x: x["box"][0])) for row in rows.values()]
    text = "\n".join(lines)
    technical_lines = [line for line in lines if re.search(r"\b(?:AXIAL|AX|COR|CORONAL|SAG|SAGITTAL)\b", line, re.I)]
    meta = technical_description(" ".join(technical_lines))
    if meta.pop("mixed_weighting"):
        issues.append({"code":"AMBIGUOUS_WEIGHTING", "severity":"error"})
    m = FILENAME.search(name)
    file_series, file_index = (int(m[1]), int(m[2])) if m else (None, None)
    def find_int(pattern):
        match = re.search(pattern, text, re.I)
        return int(match[1]) if match else None
    ocr_series = find_int(r"\b(?:SERIES|SER|SE)\s*[:#]?\s*(\d+)\b")
    n = re.search(r"\b(?:IMAGES?|IM)\s*[:#]?\s*(\d+)\s*/\s*(\d+)", text, re.I)
    ocr_index, count = (int(n[1]), int(n[2])) if n else (None, None)
    for field, a, b in [("series_no", file_series, ocr_series), ("index", file_index, ocr_index)]:
        if a is not None and b is not None and a != b:
            issues.append({"code":"OCR_FILENAME_CONFLICT", "field":field, "severity":"error"})
    meta.update(series_no=file_series if file_series is not None else ocr_series,
                index=file_index if file_index is not None else ocr_index, count=count)
    dates = set()
    for y, mo, d in re.findall(r"\b((?:19|20)\d{2})[-/.]?(\d{2})[-/.]?(\d{2})\b", text):
        try:
            from datetime import date
            dates.add(date(int(y), int(mo), int(d)).isoformat())
        except ValueError:
            pass
    # Scanner overlays also use "20-March-2019". Recognise them without adopting DOBs.
    months={name.lower():i for i,name in enumerate(["January","February","March","April","May","June","July","August","September","October","November","December"],1)}
    months.update({k[:3]:v for k,v in list(months.items())})
    for d,month,y in re.findall(r"\b(\d{1,2})[- /]([A-Za-z]{3,9})[- /]((?:19|20)\d{2})\b",text):
        if month.lower() in months:
            try:
                from datetime import date
                dates.add(date(int(y),months[month.lower()],int(d)).isoformat())
            except ValueError:pass
    # The expected date comes from an explicit study configuration. A random DOB is NEVER adopted.
    meta["date"] = expected_date
    if expected_date not in dates:
        issues.append({"code":"ACQUISITION_DATE_NOT_OCR_CONFIRMED", "severity":"warning"})
    for label, key in [("ST", "slice_thickness_mm"), ("SL", "slice_location_mm")]:
        hit = re.search(r"\b" + label + r"\s*[:=]?\s*(-?\d+(?:\.\d+)?)", text, re.I)
        meta[key] = finite_float(hit[1]) if hit else None
    wl = re.search(r"\bWL\s*[:=]?\s*(-?\d+(?:\.\d+)?)", text, re.I)
    ww = re.search(r"\bWW\s*[:=]?\s*(\d+(?:\.\d+)?)", text, re.I)
    combined = re.search(r"\bWL\s*/\s*WW\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text, re.I)
    meta["window"] = ([float(combined[1]), float(combined[2])] if combined else
                      [float(wl[1]), float(ww[1])] if wl and ww else None)
    meta["metadata_origin"] = {"date":"configured study, cross-checked against OCR",
                               "series_no":"filename + OCR", "index":"filename + OCR",
                               "technical":"OCR"}
    return meta, issues

def laterality_note(plane: str | None, verified: bool) -> str:
    if plane in ("axial", "coronal"):
        if verified:
            return "已核對放射學顯示方向：影像左側＝病人右側＝右膝外側。"
        return "標準放射學方向下：影像左側＝病人右側＝右膝外側；本片尚未核對是否翻轉，不可據此單獨定位。"
    return "矢狀面不能僅憑畫面左右判定膝內／外側；須對照定位像或 DICOM IOP/IPP。"
