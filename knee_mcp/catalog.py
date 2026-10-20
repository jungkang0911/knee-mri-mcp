"""Builds the study / series / slice index from the Drive listing."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .drive import DriveFile, DriveSource
from .errors import KneeError, NotFound
from .dicom import DicomMeta, is_dicom_candidate
from .naming import KEY_IMAGES_SERIES, ParsedName, folder_hospital, is_image_name, normalize_series, parse_name

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
        return f"{self.date}_{self.hospital}" if self.hospital else self.date

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


def build_index(
    files: list[DriveFile],
    study_types: dict[str, str],
    pii_terms: list[str],
    exclude: list[str] | None = None,
    dicom_meta: dict[str, DicomMeta | None] | None = None,
    folder_studies: dict[str, str] | None = None,
    duplicate_folders: dict[str, str] | None = None,
) -> Index:
    """folder_studies maps a folder-name fragment to a forced study id ("匿名化MRI" -> "2025-11-26_unconfirmed")
    for folders whose names cannot be trusted for the date / hospital."""
    issues: list[str] = []
    studies: dict[tuple[str, str], Study] = {}
    folder_text: dict[tuple[str, str], list[str]] = {}
    modality: dict[tuple[str, str], str] = {}
    excluded = 0
    duplicates: dict[str, int] = {}
    # DICOM first, so when the same image exists as DICOM and JPEG the DICOM copy is the one kept.
    for f in sorted(files, key=lambda x: (not is_dicom_candidate(x), x.folders, x.name)):
        dicom = is_dicom_candidate(f)
        if not dicom and not is_image_name(f.name):
            continue
        if exclude and any(term in folder for folder in f.folders for term in exclude):
            excluded += 1
            continue
        dup = next((sid for frag, sid in (duplicate_folders or {}).items() if any(frag in x for x in f.folders)), None)
        if dup:
            duplicates[dup] = duplicates.get(dup, 0) + 1
            continue
        label = _safe_label(f.name, pii_terms)
        if dicom:
            meta = (dicom_meta or {}).get(f.id)
            if meta is None:
                issues.append(f"skipped {label}: not a readable DICOM file (or missing date/series/instance)")
                continue
            if meta.frames > 1:
                issues.append(f"{label}: multi-frame DICOM ({meta.frames} frames); only the first frame is shown")
            p = ParsedName(meta.study_date, folder_hospital(f.folders), meta.series, meta.instance, True,
                           modality=meta.modality)
        else:
            p = parse_name(f.name, list(f.folders))
        if p is None:
            issues.append(f"skipped {label}: cannot determine date/series/image number")
            continue
        for frag, sid in (folder_studies or {}).items():
            if any(frag in folder for folder in f.folders):
                date, _, hosp = sid.partition("_")
                p = ParsedName(date, hosp, p.series, p.index, True, modality=p.modality)
                break
        if not p.strict:
            issues.append(f"{label}: {p.note} -> {p.date}_{p.hospital} S{p.series} I{p.index:04d}")
        key = (p.date, p.hospital)
        study = studies.setdefault(key, Study(p.date, p.hospital, "unknown"))
        folder_text.setdefault(key, []).extend(f.folders)
        if p.modality:
            modality.setdefault(key, p.modality)
        series = study.series.setdefault(p.series, Series(p.series))
        if p.series != KEY_IMAGES_SERIES and any(s.index == p.index for s in series.slices):
            issues.append(f"{label}: duplicate image I{p.index:04d} in {study.id} S{p.series}; kept the first")
            continue
        series.slices.append(Slice(p.index, f))

    for key, study in studies.items():
        for s in study.series.values():
            s.slices.sort(key=lambda x: x.index)
            if s.number == KEY_IMAGES_SERIES:  # picked images: number them 1..n in file-name order
                s.slices.sort(key=lambda x: x.file.name)
                for n, sl in enumerate(s.slices, start=1):
                    sl.index = n
        study.series = dict(sorted(study.series.items()))
        text = " ".join(folder_text.get(key, []))
        study.kind = (
            study_types.get(study.id)
            or study_types.get(study.date)
            or modality.get(key)
            or ("MRI" if _MRI_RE.search(text) else "X-ray" if _XR_RE.search(text) else "unknown")
        )
    if excluded:
        issues.append(f"excluded {excluded} files in folders matching KNEE_EXCLUDE_FOLDERS")
    for sid, n in duplicates.items():
        issues.append(f"excluded {n} files that are a duplicate copy of study {sid} (KNEE_DUPLICATE_FOLDERS); "
                      f"they are not a separate exam")
    ordered = sorted(studies.values(), key=lambda s: (s.date, s.hospital))
    return Index(ordered, issues)


class Catalog:
    """Caches the Drive listing for `ttl` seconds so new uploads (e.g. a 2026 MRI) show up automatically."""

    def __init__(
        self,
        source: DriveSource,
        study_types: dict[str, str],
        pii_terms: list[str],
        ttl: float,
        exclude: list[str] | None = None,
        dicom_loader: Callable[[list[DriveFile]], dict[str, DicomMeta | None]] | None = None,
        folder_studies: dict[str, str] | None = None,
        duplicate_folders: dict[str, str] | None = None,
    ) -> None:
        self.source = source
        self.folder_studies = folder_studies or {}
        self.duplicate_folders = duplicate_folders or {}
        self.dicom_loader = dicom_loader
        self.exclude = exclude or []
        self.study_types = study_types
        self.pii_terms = pii_terms
        self.ttl = ttl
        self._index: Index | None = None
        self._at = 0.0
        self._lock = threading.Lock()  # serializes rebuilds
        self._refreshing = False

    def _rebuild(self) -> Index:
        with self._lock:
            files = self.source.list_files()
            metas = None
            if self.dicom_loader:
                metas = self.dicom_loader([f for f in files if is_dicom_candidate(f)])
            self._index = build_index(files, self.study_types, self.pii_terms, self.exclude, metas, self.folder_studies,
                                          self.duplicate_folders)
            self._at = time.monotonic()
            return self._index

    def _background_refresh(self) -> None:
        try:
            self._rebuild()
        except Exception:  # noqa: BLE001 - keep serving the previous listing; next call retries
            pass
        finally:
            self._refreshing = False

    def index(self, refresh: bool = False) -> Index:
        """The current index. A stale index is returned immediately while a fresh one is built in the
        background (listing Drive takes ~15 s), so tool calls never pay for the periodic refresh;
        refresh=True (list_studies) or having no index yet builds synchronously."""
        idx = self._index
        if refresh or idx is None:
            return self._rebuild()
        if time.monotonic() - self._at > self.ttl and not self._refreshing:
            self._refreshing = True
            threading.Thread(target=self._background_refresh, daemon=True).start()
        return idx
