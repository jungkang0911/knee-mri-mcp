"""Tool logic: catalog lookups, cached redaction, and image composition. Returns plain Python values."""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import dicom, imaging
from .catalog import Catalog, Series, Slice, Study
from .config import Config
from .dicom import DicomMeta, is_dicom_bytes, is_dicom_candidate
from .drive import DriveFile, DriveSource, FileCache
from .errors import KneeError, NeedsReview, RedactionFailed
from .templates import load_templates
from .redact import REDACTION_VERSION, Overlay, ocr_words, read_overlay, redact

MAX_SLICES = 8


@dataclass
class Rendered:
    png: bytes
    caption: str


class DicomMetaStore:
    """Non-identifying DICOM header fields per file, cached on disk by fileId + modifiedTime.

    Reads only the first HEAD_BYTES of each file (HTTP Range) and falls back to a full download when the
    header is longer than that.
    """

    def __init__(self, cfg: Config, source: DriveSource, files: FileCache) -> None:
        self.cfg = cfg
        self.source = source
        self.files = files
        self.dir = cfg.cache_dir / "dicom-meta"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, f: DriveFile) -> Path:
        return self.dir / (hashlib.sha256(f"{f.id}|{f.modified_time}".encode()).hexdigest()[:32] + ".json")

    def get(self, f: DriveFile) -> DicomMeta | None:
        path = self._path(f)
        if path.exists():
            data = json.loads(path.read_text("utf-8"))["meta"]
            return DicomMeta(**data) if data else None
        meta = None
        head_fn = getattr(self.source, "download_head", None)
        if head_fn is not None:
            head = head_fn(f, dicom.HEAD_BYTES)
            meta = dicom.read_meta(head, self.cfg.pii_terms)
            if meta is None and len(head) >= dicom.HEAD_BYTES and is_dicom_bytes(head):
                meta = dicom.read_meta(self.files.get(self.source, f), self.cfg.pii_terms)
        else:
            meta = dicom.read_meta(self.files.get(self.source, f), self.cfg.pii_terms)
        Processor._write(path, json.dumps({"meta": meta.to_json() if meta else None}).encode())
        return meta


def _tag_overlay(meta: DicomMeta | None) -> Overlay:
    if meta is None:
        return Overlay("", "", "")
    plane = meta.description
    if meta.plane and meta.plane.upper() not in plane.upper():
        plane = f"{meta.plane} {plane}".strip()
    return Overlay(plane, meta.slice_location, meta.slice_thickness)


def _merge(tags: Overlay, ocr: Overlay) -> Overlay:
    return Overlay(tags.plane or ocr.plane, tags.sl or ocr.sl, tags.st or ocr.st)


class Processor:
    """Redacted images and overlay info, cached on disk by fileId + modifiedTime + redaction settings."""

    def __init__(
        self,
        cfg: Config,
        source: DriveSource,
        files: FileCache,
        metas: DicomMetaStore,
        template_for: Callable[[DriveFile], list | None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.source = source
        self.files = files
        self.metas = metas
        self.template_for = template_for or (lambda f: None)
        self.dir = cfg.cache_dir / "redacted"
        self.dir.mkdir(parents=True, exist_ok=True)
        salt = json.dumps([REDACTION_VERSION, cfg.ocr_langs, sorted(cfg.pii_terms), cfg.masking]).encode()
        self._salt = hashlib.sha256(salt).hexdigest()[:12]
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _key(self, f: DriveFile) -> str:
        template = json.dumps(self.template_for(f))
        return hashlib.sha256(f"{f.id}|{f.modified_time}|{self._salt}|{template}".encode()).hexdigest()[:32]

    def png_path(self, f: DriveFile) -> Path:
        return self.dir / f"{self._key(f)}.png"

    def info(self, f: DriveFile) -> dict:
        """The stored result for this image ({} when not prebuilt): status, overlay, mask_boxes, review_boxes."""
        meta = self.dir / f"{self._key(f)}.json"
        return json.loads(meta.read_text("utf-8")) if meta.exists() else {}

    def _approved(self) -> dict[str, str]:
        path = self.cfg.cache_dir / "review_decisions.json"
        return json.loads(path.read_text("utf-8")) if path.exists() else {}

    def decide_review(self, f: DriveFile, decision: str) -> None:
        """Owner decision for a "review" image: "approve" shows it as is, "mask" blacks out the flagged boxes."""
        if decision not in ("approve", "mask"):
            raise KneeError("decision must be approve or mask")
        key = self._key(f)
        with self._lock(key):
            info = self.info(f)
            if info.get("status") != "review":
                raise KneeError("this image is not waiting for review")
            if decision == "mask":
                with Image.open(self.png_path(f)) as im:
                    im.load()
                    img = im.copy()
                boxes = [list(b) for b in info.get("review_boxes", [])]
                from .redact import _blackout

                _blackout(img, boxes)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                self._write(self.png_path(f), buf.getvalue())
                info["mask_boxes"] = info.get("mask_boxes", []) + boxes
            info["status"] = "ok"
            info["review_decision"] = decision
            self._write(self.dir / f"{key}.json", json.dumps(info).encode())

    def status(self, f: DriveFile) -> str:
        """"ok" / "failed" (withheld) / "review" (waiting for the owner) / "todo" (not prebuilt yet)."""
        st = self.info(f).get("status")
        if st in ("ok", "review") and not self.png_path(f).exists():
            return "todo"
        return st if st in ("ok", "failed", "review") else "todo"

    def cached_overlay(self, f: DriveFile) -> Overlay | None:
        """Overlay info without any download or OCR (None when the image has not been prebuilt)."""
        if is_dicom_candidate(f):
            return _tag_overlay(self.metas.get(f))
        meta = self.dir / f"{self._key(f)}.json"
        if not meta.exists():
            return None
        return Overlay(**json.loads(meta.read_text("utf-8"))["overlay"])

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        tmp = path.with_suffix(path.suffix + f".{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def overlay(self, f: DriveFile) -> Overlay:
        """Plane / SL / ST only (one OCR pass on the original; nothing but allowlisted tokens comes out)."""
        key = self._key(f)
        meta = self.dir / f"{key}.json"
        with self._lock(key):
            if meta.exists():
                return Overlay(**json.loads(meta.read_text("utf-8"))["overlay"])
            if is_dicom_candidate(f):
                return _tag_overlay(self.metas.get(f))  # header tags beat OCR; nothing to cache here
            img = Image.open(io.BytesIO(self.files.get(self.source, f)))
            ov = read_overlay(ocr_words(img, self.cfg.ocr_langs))
            self._write(meta, json.dumps({"overlay": ov.__dict__, "status": "overlay"}).encode())
            return ov

    def is_cached(self, f: DriveFile) -> bool:
        """True when redacted() can answer from disk (masked, withheld or waiting for review)."""
        return self.status(f) != "todo"

    def redacted(self, f: DriveFile, raw: bytes | None = None) -> tuple[Image.Image, Overlay]:
        key = self._key(f)
        png, meta = self.dir / f"{key}.png", self.dir / f"{key}.json"
        with self._lock(key):
            if meta.exists():
                info = json.loads(meta.read_text("utf-8"))
                if info.get("status") == "failed":
                    raise RedactionFailed()
                if info.get("status") == "review" and png.exists():
                    raise NeedsReview()
                if info.get("status") == "ok" and png.exists():
                    with Image.open(png) as im:
                        im.load()
                        return im.copy(), Overlay(**info["overlay"])
            if raw is None:
                raw = self.files.get(self.source, f)
            tags = Overlay("", "", "")
            source_img: bytes | Image.Image = raw
            if is_dicom_bytes(raw):
                try:
                    source_img = dicom.to_image(raw)
                except Exception:  # noqa: BLE001 - unsupported transfer syntax or broken pixel data
                    raise KneeError("cannot decode this DICOM image (unsupported compression or pixel format)") from None
                tags = _tag_overlay(dicom.read_meta(raw, self.cfg.pii_terms))
            del raw
            if not self.cfg.masking:
                # Masking disabled (owner's own data): one quick OCR pass for plane / SL / ST, nothing masked.
                img = source_img if isinstance(source_img, Image.Image) else Image.open(io.BytesIO(source_img))
                img.load()
                ov = _merge(tags, read_overlay(ocr_words(img, self.cfg.ocr_langs, fast=True)))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                self._write(png, buf.getvalue())
                self._write(meta, json.dumps({"status": "ok", "overlay": ov.__dict__, "mask_boxes": [],
                                              "review_boxes": [], "masking": False}).encode())
                return img, ov
            try:
                result = redact(source_img, self.cfg.ocr_langs, self.cfg.pii_terms, self.template_for(f))
            except RedactionFailed:
                self._write(meta, json.dumps({"status": "failed", "overlay": Overlay("", "", "").__dict__}).encode())
                raise
            img, ov = result.img, _merge(tags, result.overlay)
            status = "review" if result.review_boxes else "ok"
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self._write(png, buf.getvalue())
            self._write(meta, json.dumps({
                "status": status, "overlay": ov.__dict__,
                "mask_boxes": result.mask_boxes, "review_boxes": result.review_boxes,
            }).encode())
            if status == "review":
                raise NeedsReview()
            return img, ov


def vote_plane(readings: list[str]) -> str:
    """Series name from many per-image OCR readings: tokens seen in >= 30% of readings (min 2) are kept,
    e.g. 10x "SAG PD" + 6x "SAG PD FS" -> "SAG PD FS"."""
    readings = [r for r in readings if r]
    if not readings:
        return ""
    counts = Counter(t for r in readings for t in dict.fromkeys(r.split()))
    need = len(readings) / 2 if len(readings) < 5 else max(2, 0.3 * len(readings))  # few readings: majority
    keep = {t for t, n in counts.items() if n >= need} or {counts.most_common(1)[0][0]}
    best = max(readings, key=lambda r: (len([t for t in r.split() if t in keep]), readings.count(r)))
    ordered = [t for t in dict.fromkeys(best.split()) if t in keep]
    ordered += [t for t, _ in counts.most_common() if t in keep and t not in ordered]
    return " ".join(ordered)


_FOLDER_WORDS = [
    ("冠狀", "COR"), ("矢狀", "SAG"), ("橫切", "Axial"), ("軸狀", "Axial"),
    ("COR", "COR"), ("SAG", "SAG"), ("AX", "Axial"),
    ("PD", "PD"), ("T1", "T1"), ("T2", "T2"), ("脂肪抑制", "FS"), ("FS", "FS"), ("FSE", "FSE"), ("TSE", "TSE"),
    ("STIR", "STIR"), ("3D", "3D"), ("MERGE", "MERGE"), ("DESS", "DESS"),
]


def plane_from_folders(folders: tuple[str, ...] | list[str]) -> str:
    """Series name from a folder such as "冠狀面_T2脂肪抑制FSE" -> "COR T2 FS FSE" ("" if none)."""
    name = (folders[-1] if folders else "").upper()
    found: list[tuple[int, str]] = []
    for word, token in _FOLDER_WORDS:
        pos = name.find(word.upper())
        while pos >= 0:
            # "FS" inside "FSE" is not fat saturation; "AX" must not match inside other words
            if not (word in ("FS", "AX") and name[pos:pos + 3] in ("FSE", "AXI") and token != "Axial"):
                found.append((pos, token))
            pos = name.find(word.upper(), pos + 1)
    tokens = list(dict.fromkeys(t for _, t in sorted(found)))
    planes = [t for t in tokens if t in ("COR", "SAG", "Axial")]
    return " ".join(planes[:1] + [t for t in tokens if t not in ("COR", "SAG", "Axial")]) if tokens else ""


def repair_sl(values: dict[int, str]) -> dict[int, str]:
    """Fix OCR'd slice locations within one series.

    Slice locations change linearly with the image number. A Theil-Sen line (median of all pairwise
    slopes) through the readings is robust to the misreads seen on real images ("39.91" -> "3",
    "84.23" -> "8423"). A reading that fits the line is kept; one that fits after restoring a dropped
    decimal point is rescaled; anything else, and missing values, is taken from the line and marked "~".
    Large absolute values are fine (axial positions such as 424.3 mm are real)."""
    nums: dict[int, float] = {}
    for idx, v in values.items():
        try:
            nums[idx] = float(v)
        except (TypeError, ValueError):
            pass
    if len(nums) < 3:
        return {i: f"{v:g}" for i, v in nums.items()}
    pts = sorted(nums.items())
    slopes = sorted((b[1] - a[1]) / (b[0] - a[0]) for i, a in enumerate(pts) for b in pts[i + 1:] if b[0] != a[0])
    slope = slopes[len(slopes) // 2]
    intercept = sorted(v - slope * i for i, v in pts)[len(pts) // 2]
    tol = max(0.6, abs(slope) * 0.5)
    out: dict[int, str] = {}
    for idx in sorted(set(values) | set(nums)):
        pred = intercept + slope * idx
        v = nums.get(idx)
        fixed = None
        if v is not None:
            for cand in (v, v / 10, v / 100):
                if abs(cand - pred) <= tol:
                    fixed = cand
                    break
        out[idx] = f"{round(fixed, 2):g}" if fixed is not None else f"~{round(pred, 1):g}"
    return out


class KneeService:
    def __init__(self, cfg: Config, source: DriveSource) -> None:
        self.cfg = cfg
        files = FileCache(cfg.cache_dir, keep=cfg.keep_raw)
        self.metas = DicomMetaStore(cfg, source, files)
        self.pool = ThreadPoolExecutor(max_workers=max(1, cfg.workers))
        self._inflight: dict[str, Future] = {}
        self._inflight_lock = threading.Lock()
        self.catalog = Catalog(
            source, cfg.study_types, cfg.pii_terms, cfg.listing_ttl, cfg.exclude_folders, self._load_dicom_metas,
            cfg.folder_studies,
        )
        self.templates = load_templates(cfg.mask_templates)
        self._hosp_index: object = None
        self._hosp_by_id: dict[str, str] = {}
        self.proc = Processor(cfg, source, files, self.metas, self._template_for)

    def series_profile(self, se: Series) -> tuple[str, dict[int, str]]:
        """(voted series name, repaired SL per image number) from the prebuilt per-image readings."""
        ovs = {s.index: self.proc.cached_overlay(s.file) for s in se.slices}
        readings = [o.plane for o in ovs.values() if o]
        plane = vote_plane(readings)
        folder = plane_from_folders(se.slices[0].file.folders) if se.slices else ""
        if folder:
            # The folder name is one reliable reading; it also supplies the plane when the overlay has none.
            plane = vote_plane(readings + [folder] * max(1, len(readings) // 3)) if readings else folder
            if not any(t in plane.split() for t in ("COR", "SAG", "Axial")):
                folder_plane = next((t for t in folder.split() if t in ("COR", "SAG", "Axial")), "")
                plane = f"{folder_plane} {plane}".strip()
        sls = repair_sl({i: (o.sl if o else "") for i, o in ovs.items()})  # unread SL filled from the trend
        return plane, sls

    def _caption(self, study: Study, series: Series, sl: Slice, ov: Overlay) -> str:
        plane, sls = self.series_profile(series)
        boxes = len(self.proc.info(sl.file).get("mask_boxes", []))
        parts = [f"{study.id} S{series.number} I{sl.index:04d}", plane or ov.plane or "plane ?"]
        parts.append(f"SL {sls[sl.index]}" if sl.index in sls else "SL ?")
        parts.append(f"ST {ov.st}" if ov.st else "ST ?")
        parts.append(f"masked boxes {boxes}")
        return " | ".join(parts)

    def _template_for(self, f: DriveFile) -> list | None:
        """Mask template of the hospital the file's study belongs to (None: OCR-only masking)."""
        idx = self.catalog.index()
        if idx is not self._hosp_index:
            self._hosp_by_id = {s.file.id: st.hospital for st in idx.studies for se in st.series.values() for s in se.slices}
            self._hosp_index = idx
        rects = self.templates.get(self._hosp_by_id.get(f.id, ""))
        return [list(r) for r in rects] if rects else None

    def _load_dicom_metas(self, files: list[DriveFile]) -> dict[str, DicomMeta | None]:
        def one(f: DriveFile) -> DicomMeta | None:
            try:
                return self.metas.get(f)
            except Exception:  # noqa: BLE001 - reported as an unreadable file in issues
                return None

        # Header reads are network-bound, so use more threads than the CPU-bound OCR pool.
        with ThreadPoolExecutor(max_workers=8) as pool:
            return dict(zip((f.id for f in files), pool.map(one, files)))

    # ---- lookups -------------------------------------------------------------
    def _resolve(self, study: str, series: str | int) -> tuple[Study, Series]:
        st = self.catalog.index().get_study(study)
        return st, st.get_series(series)

    # ---- tools ---------------------------------------------------------------
    def list_studies(self) -> str:
        idx = self.catalog.index(refresh=True)
        if not idx.studies:
            lines = ["No studies found under the configured Drive folder."]
        else:
            lines = ["study_id | date | hospital | type | series | images"]
            for s in idx.studies:
                n = sum(len(x.slices) for x in s.series.values())
                lines.append(f"{s.id} | {s.date} | {s.hospital or '-'} | {s.kind} | {len(s.series)} | {n}")
        if not self.cfg.masking:
            lines.append("")
            lines.append("NOTE: masking is DISABLED (KNEE_MASKING=0); images are served as stored, including any "
                         "burned-in patient details")
        if idx.issues:
            lines.append("")
            lines.append(f"issues ({len(idx.issues)}):")
            lines.extend(f"- {i}" for i in idx.issues[:50])
            if len(idx.issues) > 50:
                lines.append(f"- ... {len(idx.issues) - 50} more")
        return "\n".join(lines)

    def list_series(self, study: str) -> str:
        """Summarized from prebuilt results only: no download, no OCR at request time."""
        st = self.catalog.index().get_study(study)
        lines = [f"{st.id} ({st.kind})", "series | images | I range | plane (voted) | SL range | prebuilt | review"]
        todo_total = 0
        for se in st.series.values():
            statuses = [self.proc.status(s.file) for s in se.slices]
            done = sum(1 for x in statuses if x != "todo")
            todo_total += len(se.slices) - done
            plane, sls = self.series_profile(se)
            vals = [float(v) for v in sls.values() if not v.startswith("~")]
            sl_range = f"{min(vals):g} .. {max(vals):g}" if len(vals) >= 2 else (f"{vals[0]:g}" if vals else "?")
            lines.append(
                f"S{se.number} | {len(se.slices)} | I{se.slices[0].index:04d}-I{se.slices[-1].index:04d}"
                f" | {plane or '?'} | {sl_range} | {done}/{len(se.slices)} | {statuses.count('review')}"
            )
        if todo_total:
            lines.append(f"{todo_total} images not prebuilt yet; plane/SL fill in as `python -m knee_mcp.prebuild` runs")
        return "\n".join(lines)

    def _safe_overlay(self, f: DriveFile) -> Overlay | None:
        try:
            return self.proc.overlay(f)
        except Exception:  # noqa: BLE001 - overlay text is best effort
            return None

    # ---- background redaction with a response-time budget ----------------------
    # First-time redaction of one image takes 20-60 s on a small server. Tools wait at most
    # cfg.tool_budget seconds, return what is ready, and leave the rest running so a repeat call is fast.

    def _submit(self, f: DriveFile) -> Future:
        with self._inflight_lock:
            fut = self._inflight.get(f.id)
            if fut is None:
                fut = self.pool.submit(self.proc.redacted, f)
                self._inflight[f.id] = fut
                fut.add_done_callback(lambda done, key=f.id: self._forget(key, done))
            return fut

    def _forget(self, key: str, fut: Future) -> None:
        with self._inflight_lock:
            if self._inflight.get(key) is fut:
                del self._inflight[key]

    def _gather(self, files: list[DriveFile]) -> list[tuple[str, object]]:
        """Per file: ("ok", (img, overlay)) | ("withheld", None) | ("pending", None) | ("error", message)."""
        # Finished results are read straight from disk so they never queue behind slow first-time jobs.
        futs: list[Future] = []
        for f in files:
            if self.proc.is_cached(f):
                fut: Future = Future()
                try:
                    fut.set_result(self.proc.redacted(f))
                except Exception as e:  # noqa: BLE001 - surfaced per file below
                    fut.set_exception(e)
                futs.append(fut)
            else:
                futs.append(self._submit(f))
        wait(futs, timeout=self.cfg.tool_budget)
        out: list[tuple[str, object]] = []
        for fut in futs:
            if not fut.done():
                out.append(("pending", None))
                continue
            exc = fut.exception()
            if exc is None:
                out.append(("ok", fut.result()))
            elif isinstance(exc, RedactionFailed):
                out.append(("withheld", None))
            elif isinstance(exc, KneeError):
                out.append(("error", str(exc)))
            else:
                raise exc
        return out

    def _one_ready(self, f: DriveFile, label: str) -> tuple[Image.Image, Overlay]:
        state, val = self._gather([f])[0]
        if state == "ok":
            return val  # type: ignore[return-value]
        if state == "pending":
            raise KneeError(f"{label} is not prebuilt yet (queued for masking); "
                            "try again in a few minutes, or run `python -m knee_mcp.prebuild`")
        if state == "withheld":
            raise RedactionFailed()
        raise KneeError(str(val))

    def contact_sheet(self, study: str, series: str | int, cols: int = 5) -> Rendered:
        st, se = self._resolve(study, series)
        results = self._gather([s.file for s in se.slices])
        tiles = []
        for s, (state, val) in zip(se.slices, results):
            img = val[0] if state == "ok" else imaging.PENDING if state == "pending" else None  # type: ignore[index]
            tiles.append((f"I{s.index:04d}", img))
        sheet = imaging.contact_sheet(tiles, cols)
        caption = f"{st.id} S{se.number}: {len(tiles)} images, I{se.slices[0].index:04d}-I{se.slices[-1].index:04d}"
        withheld = sum(1 for state, _ in results if state in ("withheld", "error"))
        pending = sum(1 for state, _ in results if state == "pending")
        if withheld:
            caption += f"; {withheld} withheld (redaction check failed)"
        if pending:
            caption += f"; {pending} not prebuilt yet (tiles say 'masking...'); call get_contact_sheet again in a few minutes"
        return Rendered(imaging.to_png(sheet, self.cfg.max_png_bytes), caption)

    def _try_redacted(self, f: DriveFile) -> Image.Image | None:
        try:
            return self.proc.redacted(f)[0]
        except RedactionFailed:
            return None

    def slice(self, study: str, series: str | int, index: int, crop=None, zoom: float = 1) -> Rendered:
        st, se = self._resolve(study, series)
        sl = se.get(int(index))
        img, ov = self._one_ready(sl.file, f"I{sl.index:04d}")
        out = imaging.apply_zoom(imaging.apply_crop(img, crop), zoom)
        caption = self._caption(st, se, sl, ov) + f" | size {img.width}x{img.height}"
        if crop:
            caption += f" | crop {list(crop)}"
        return Rendered(imaging.to_png(out, self.cfg.max_png_bytes), caption)

    def slices(self, study: str, series: str | int, start: int, end: int, crop=None) -> list[Rendered]:
        """Ready images as PNG; images still being masked or withheld come back as text-only entries (png=b"")."""
        st, se = self._resolve(study, series)
        lo, hi = sorted((int(start), int(end)))
        chosen = [s for s in se.slices if lo <= s.index <= hi]
        if not chosen:
            raise KneeError(f"no images between I{lo:04d} and I{hi:04d} in S{se.number}")
        if len(chosen) > MAX_SLICES:
            raise KneeError(f"range has {len(chosen)} images; at most {MAX_SLICES} per call")
        out: list[Rendered] = []
        pending: list[str] = []
        results = self._gather([s.file for s in chosen])
        ready = sum(1 for state, _ in results if state == "ok")
        per_image = self.cfg.max_png_bytes // max(1, ready)  # the whole response shares one size budget
        for sl, (state, val) in zip(chosen, results):
            label = f"I{sl.index:04d}"
            if state == "ok":
                img, ov = val  # type: ignore[misc]
                png = imaging.to_png(imaging.apply_crop(img, crop), per_image)
                out.append(Rendered(png, self._caption(st, se, sl, ov)))
            elif state == "pending":
                pending.append(label)
            elif state == "withheld":
                out.append(Rendered(b"", f"{st.id} S{se.number} {label}: withheld (redaction check failed)"))
            else:
                out.append(Rendered(b"", f"{st.id} S{se.number} {label}: {val}"))
        if pending:
            out.append(Rendered(b"", f"not prebuilt yet (queued for masking): {', '.join(pending)}; "
                                     "call get_slices again for these in a few minutes"))
        return out

    def compare(self, a: dict, b: dict) -> Rendered:
        refs = []
        for ref in (a, b):
            st, se = self._resolve(ref["study"], ref["series"])
            refs.append((st, se, se.get(int(ref["index"])), ref.get("crop")))
        results = self._gather([sl.file for _, _, sl, _ in refs])
        pending = [f"{st.date} I{sl.index:04d}" for (st, _, sl, _), (state, _) in zip(refs, results) if state == "pending"]
        if pending:
            raise KneeError(f"not prebuilt yet (queued for masking): {', '.join(pending)}; call compare again in a few minutes")
        imgs, labels = [], []
        for (st, se, sl, crop), (state, val) in zip(refs, results):
            if state == "withheld":
                raise RedactionFailed()
            if state == "error":
                raise KneeError(str(val))
            img, ov = val  # type: ignore[misc]
            imgs.append(imaging.apply_crop(img, crop))
            labels.append(" ".join(x for x in (st.date, st.hospital, f"S{se.number}", f"I{sl.index:04d}", ov.plane) if x))
        out = imaging.side_by_side(imgs[0], labels[0], imgs[1], labels[1])
        return Rendered(imaging.to_png(out, self.cfg.max_png_bytes), f"left: {labels[0]} | right: {labels[1]}")

    def warm(self) -> tuple[int, int]:
        """Back-compat for `--warm`: prebuild everything. Returns (ok, withheld)."""
        m = self.prebuild()
        return sum(e["redaction_status"] == "ok" for e in m), sum(e["redaction_status"] != "ok" for e in m)

    def prebuild(
        self,
        progress: Callable[[str], None] | None = None,
        download_threads: int = 4,
        debug_dir: Path | None = None,
    ) -> list[dict]:
        """Mask every image ahead of time and write <cache>/manifest.json.

        Incremental: images whose fileId + modifiedTime (and mask settings) were already processed are
        skipped. Downloads run in parallel; OCR uses cfg.workers threads."""
        say = progress or (lambda msg: None)
        idx = self.catalog.index(refresh=True)
        items = [(st, se, sl) for st in idx.studies for se in st.series.values() for sl in se.slices]
        todo = [it for it in items if self.proc.status(it[2].file) == "todo"]
        say(f"{len(items)} images, {len(items) - len(todo)} already prebuilt, {len(todo)} to do")

        def fetch(it):
            try:
                return it, self.proc.files.get(self.proc.source, it[2].file), None
            except Exception as e:  # noqa: BLE001 - reported per image via its status
                return it, None, e

        def work(entry) -> None:
            it, raw, err = entry
            if err is None:
                try:
                    self.proc.redacted(it[2].file, raw)
                except KneeError:  # withheld / needs review / undecodable: recorded in its status
                    pass

        done = 0
        chunk = max(download_threads * 2, self.cfg.workers * 2)
        with ThreadPoolExecutor(max_workers=download_threads) as dl:
            for start in range(0, len(todo), chunk):
                fetched = list(dl.map(fetch, todo[start:start + chunk]))
                list(self.pool.map(work, fetched))
                done += len(fetched)
                say(f"{done}/{len(todo)} processed")

        manifest = []
        profiles = {}
        for st, se, sl in items:
            if id(se) not in profiles:
                profiles[id(se)] = self.series_profile(se)
            plane, sls = profiles[id(se)]
            status = self.proc.status(sl.file)
            info = self.proc.info(sl.file)
            ov = self.proc.cached_overlay(sl.file)
            size = None
            if status in ("ok", "review"):
                with Image.open(self.proc.png_path(sl.file)) as im:
                    size = list(im.size)
                    if debug_dir is not None:
                        _debug_overlay(im, info, debug_dir / f"{st.id}_S{se.number}_I{sl.index:04d}.png")
            manifest.append({
                "study": st.id, "series": se.number, "index": sl.index,
                "plane": plane or (ov.plane if ov else ""), "sl": sls.get(sl.index, ""), "st": ov.st if ov else "",
                "size": size, "fileId": sl.file.id, "modifiedTime": sl.file.modified_time,
                "masked_path": f"redacted/{self.proc.png_path(sl.file).name}" if status in ("ok", "review") else None,
                "redaction_status": "withheld" if status == "failed" else status,
                "mask_boxes": info.get("mask_boxes", []),
                "review_boxes": info.get("review_boxes", []),
            })
        Processor._write(self.cfg.cache_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1).encode())
        say("manifest.json written")
        return manifest


def _debug_overlay(masked: Image.Image, info: dict, path: Path) -> None:
    """Masked image with every blacked-out box outlined in red and review boxes in yellow, for checking.

    Drawn on the masked image, so it shows no more than the tools do (review boxes are not masked: keep the
    debug folder private)."""
    from PIL import ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    img = masked.convert("RGB")
    draw = ImageDraw.Draw(img)
    for b in info.get("mask_boxes", []):
        draw.rectangle((b[0], b[1], b[2] - 1, b[3] - 1), outline=(255, 40, 40), width=2)
    for b in info.get("review_boxes", []):
        draw.rectangle((b[0] - 2, b[1] - 2, b[2] + 1, b[3] + 1), outline=(255, 220, 0), width=2)
    img.save(path)
