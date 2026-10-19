"""Builds the study / series / slice index from the Drive listing."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from .drive import DriveFile, DriveSource
from .errors import KneeError, NotFound
from .naming import is_image_name, normalize_series, parse_name

_MRI_RE = re.compile(r"(?<![A-Za-z])(MRI|MR)(?![A-Za-z])", re.IGNORECASE)
_XR_RE = re.compile(r"(?<![A-Za-z])(X-?RAY|XR|CR|DX)(?![A-Za-z])|X光", re.IGNORECASE)


@dataclass
class Slice:
    index: int
    file: DriveFile


@dataclass
class Series:
    number: int
    slices: list[Slice] = field(default_factory=list)

    def get(self, index: int) -> Slice:
        for s in self.slices:
            if s.index == index:
                return s
        raise NotFound(f"image I{index:04d} not found in series S{self.number}")


@dataclass
class Study:
    date: str
    hospital: str
    kind: str
    series: dict[int, Series] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.date}_{self.hospital}"

    def get_series(self, value: str | int) -> Series:
        try:
            num = normalize_series(value)
        except ValueError as e:
            raise KneeError(str(e)) from None
        if num not in self.series:
            raise NotFound(f"series S{num} not found in study {self.id}")
        return self.series[num]


@dataclass
class Index:
    studies: list[Study]
    issues: list[str]

    def get_study(self, key: str) -> Study:
        k = (key or "").strip().upper()
        exact = [s for s in self.studies if s.id.upper() == k]
        if exact:
            return exact[0]
        by_date = [s for s in self.studies if s.date == k]
        if len(by_date) == 1:
            return by_date[0]
        if len(by_date) > 1:
            raise KneeError(f"several studies on {k}; use one of: " + ", ".join(s.id for s in by_date))
        raise NotFound("study not found; call list_studies for valid ids")


def _safe_label(name: str, pii_terms: list[str]) -> str:
    """A file name safe to show in issues: no PII terms, no paths, bounded length."""
    low = name.lower()
    if any(t and t.lower() in low for t in pii_terms) or not re.fullmatch(r"[\w.\- ()]{1,80}", name, re.ASCII):
        return "(name withheld)"
    return name


def build_index(files: list[DriveFile], study_types: dict[str, str], pii_terms: list[str]) -> Index:
    issues: list[str] = []
    studies: dict[tuple[str, str], Study] = {}
    folder_text: dict[tuple[str, str], list[str]] = {}
    for f in sorted(files, key=lambda x: (x.folders, x.name)):
        if not is_image_name(f.name):
            continue
        p = parse_name(f.name, list(f.folders))
        label = _safe_label(f.name, pii_terms)
        if p is None:
            issues.append(f"skipped {label}: cannot determine date/series/image number")
            continue
        if not p.strict:
            issues.append(f"{label}: {p.note} -> {p.date}_{p.hospital} S{p.series} I{p.index:04d}")
        key = (p.date, p.hospital)
        study = studies.setdefault(key, Study(p.date, p.hospital, "unknown"))
        folder_text.setdefault(key, []).extend(f.folders)
        series = study.series.setdefault(p.series, Series(p.series))
        if any(s.index == p.index for s in series.slices):
            issues.append(f"{label}: duplicate image I{p.index:04d} in {study.id} S{p.series}; kept the first")
            continue
        series.slices.append(Slice(p.index, f))

    for key, study in studies.items():
        for s in study.series.values():
            s.slices.sort(key=lambda x: x.index)
        study.series = dict(sorted(study.series.items()))
        text = " ".join(folder_text.get(key, []))
        study.kind = (
            study_types.get(study.id)
            or study_types.get(study.date)
            or ("MRI" if _MRI_RE.search(text) else "X-ray" if _XR_RE.search(text) else "unknown")
        )
    ordered = sorted(studies.values(), key=lambda s: (s.date, s.hospital))
    return Index(ordered, issues)


class Catalog:
    """Caches the Drive listing for `ttl` seconds so new uploads (e.g. a 2026 MRI) show up automatically."""

    def __init__(self, source: DriveSource, study_types: dict[str, str], pii_terms: list[str], ttl: float) -> None:
        self.source = source
        self.study_types = study_types
        self.pii_terms = pii_terms
        self.ttl = ttl
        self._index: Index | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def index(self, refresh: bool = False) -> Index:
        with self._lock:
            if refresh or self._index is None or time.monotonic() - self._at > self.ttl:
                self._index = build_index(self.source.list_files(), self.study_types, self.pii_terms)
                self._at = time.monotonic()
            return self._index
