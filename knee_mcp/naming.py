"""Filename parsing: {YYYY-MM-DD}_{HOSP}_RK_S{series}_I{index:04}.jpg, with a folder-name fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass

STRICT_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<hosp>[A-Za-z][A-Za-z0-9]*)_RK_S(?P<series>\d+)_I(?P<index>\d{4})\.jpe?g$",
    re.IGNORECASE,
)

_DATE_RES = [
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})[._/](\d{2})[._/](\d{2})(?!\d)"),
]
_SERIES_RES = [
    re.compile(r"(?:^|[^A-Za-z])S(?:E|ERIES)?[ _-]?0*(\d{1,3})(?![0-9])", re.IGNORECASE),
]
_HOSP_RE = re.compile(r"(?:^|[_\s-])([A-Z]{3,8})(?=$|[_\s-])")
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png)$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedName:
    date: str
    hospital: str
    series: int
    index: int
    strict: bool
    note: str = ""


def normalize_series(value: str | int) -> int:
    """'S04', 's4', '4', 4 -> 4. Raises ValueError otherwise."""
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"\s*(?:S(?:E|ERIES)?)?\s*0*(\d+)\s*", str(value), re.IGNORECASE)
    if not m:
        raise ValueError("series must look like 7, S7 or S07")
    return int(m.group(1))


def is_image_name(name: str) -> bool:
    return bool(_IMAGE_EXT_RE.search(name))


def _find_date(text: str) -> str | None:
    for rx in _DATE_RES:
        m = rx.search(text)
        if m:
            y, mo, d = m.groups()
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                return f"{y}-{mo}-{d}"
    return None


def parse_name(name: str, folders: list[str]) -> ParsedName | None:
    """Parse a file name; `folders` is the path from the root folder (outermost first).

    Returns None when neither the file name nor its folders give a date, series and index.
    """
    m = STRICT_RE.match(name)
    if m:
        return ParsedName(
            date=m["date"],
            hospital=m["hosp"].upper(),
            series=int(m["series"]),
            index=int(m["index"]),
            strict=True,
        )

    # Fallback: date / hospital / series from the folders (innermost wins), index from the file name.
    date = hosp = None
    series = None
    for folder in reversed(folders):
        date = date or _find_date(folder)
        if series is None:
            for rx in _SERIES_RES:
                sm = rx.search(folder)
                if sm:
                    series = int(sm.group(1))
                    break
        if hosp is None:
            hm = _HOSP_RE.search(folder)
            if hm and hm.group(1) not in {"MRI", "RK", "LK", "XR", "CT", "MR", "DICOM", "JPG", "IMG"}:
                hosp = hm.group(1)
    stem = _IMAGE_EXT_RE.sub("", name)
    if date is None:
        date = _find_date(stem)
    nums = re.findall(r"\d+", stem)
    if series is None and len(nums) >= 2 and not _find_date(stem):
        # img-00005-00013 style: first number = series, last = image.
        series = int(nums[-2])
    if not nums or date is None or series is None:
        return None
    return ParsedName(
        date=date,
        hospital=(hosp or "UNKNOWN"),
        series=series,
        index=int(nums[-1]),
        strict=False,
        note="name does not follow the naming rule; date/hospital/series inferred from folder names",
    )
