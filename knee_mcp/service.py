"""Tool logic: catalog lookups, cached redaction, and image composition. Returns plain Python values."""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import dicom, imaging
from .catalog import Catalog, Series, Slice, Study
from .config import Config
from .dicom import DicomMeta, is_dicom_bytes, is_dicom_candidate
from .drive import DriveFile, DriveSource, FileCache
from .errors import KneeError, RedactionFailed
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

    def __init__(self, cfg: Config, source: DriveSource, files: FileCache, metas: DicomMetaStore) -> None:
        self.cfg = cfg
        self.source = source
        self.files = files
        self.metas = metas
        self.dir = cfg.cache_dir / "redacted"
        self.dir.mkdir(parents=True, exist_ok=True)
        salt = json.dumps([REDACTION_VERSION, cfg.ocr_langs, sorted(cfg.pii_terms)]).encode()
        self._salt = hashlib.sha256(salt).hexdigest()[:12]
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _key(self, f: DriveFile) -> str:
        return hashlib.sha256(f"{f.id}|{f.modified_time}|{self._salt}".encode()).hexdigest()[:32]

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
        """True when redacted() can answer from disk (a finished result, masked or withheld)."""
        meta = self.dir / f"{self._key(f)}.json"
        if not meta.exists():
            return False
        status = json.loads(meta.read_text("utf-8")).get("status")
        return status == "failed" or (status == "ok" and (self.dir / f"{self._key(f)}.png").exists())

    def redacted(self, f: DriveFile) -> tuple[Image.Image, Overlay]:
        key = self._key(f)
        png, meta = self.dir / f"{key}.png", self.dir / f"{key}.json"
        with self._lock(key):
            if meta.exists():
                info = json.loads(meta.read_text("utf-8"))
                if info.get("status") == "failed":
                    raise RedactionFailed()
                if info.get("status") == "ok" and png.exists():
                    with Image.open(png) as im:
                        im.load()
                        return im.copy(), Overlay(**info["overlay"])
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
            try:
                img, ov = redact(source_img, self.cfg.ocr_langs, self.cfg.pii_terms)
                ov = _merge(tags, ov)
            except RedactionFailed:
                self._write(meta, json.dumps({"status": "failed", "overlay": Overlay("", "", "").__dict__}).encode())
                raise
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self._write(png, buf.getvalue())
            self._write(meta, json.dumps({"status": "ok", "overlay": ov.__dict__}).encode())
            return img, ov


def _caption(study: Study, series: Series, sl: Slice, ov: Overlay) -> str:
    parts = [f"{study.id} S{series.number} I{sl.index:04d}", ov.plane or "plane ?"]
    parts.append(f"SL {ov.sl}" if ov.sl else "SL ?")
    parts.append(f"ST {ov.st}" if ov.st else "ST ?")
    return " | ".join(parts)


class KneeService:
    def __init__(self, cfg: Config, source: DriveSource) -> None:
        self.cfg = cfg
        files = FileCache(cfg.cache_dir, keep=cfg.keep_raw)
        self.metas = DicomMetaStore(cfg, source, files)
        self.pool = ThreadPoolExecutor(max_workers=max(1, cfg.workers))
        self._inflight: dict[str, Future] = {}
        self._inflight_lock = threading.Lock()
        self.catalog = Catalog(
            source, cfg.study_types, cfg.pii_terms, cfg.listing_ttl, cfg.exclude_folders, self._load_dicom_metas
        )
        self.proc = Processor(cfg, source, files, self.metas)

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
        if idx.issues:
            lines.append("")
            lines.append(f"issues ({len(idx.issues)}):")
            lines.extend(f"- {i}" for i in idx.issues[:50])
            if len(idx.issues) > 50:
                lines.append(f"- ... {len(idx.issues) - 50} more")
        return "\n".join(lines)

    def list_series(self, study: str) -> str:
        st = self.catalog.index().get_study(study)
        lines = [f"{st.id} ({st.kind})", "series | images | I range | plane (overlay OCR) | SL range"]
        for se in st.series.values():
            # Plane from the middle image, SL range from the first and last images.
            picks = [se.slices[0], se.slices[len(se.slices) // 2], se.slices[-1]]
            ovs = list(self.pool.map(lambda s: self._safe_overlay(s.file), picks))
            planes = Counter(o.plane for o in ovs if o and o.plane)
            plane = planes.most_common(1)[0][0] if planes else "?"
            sls = [float(o.sl) for o in (ovs[0], ovs[-1]) if o and o.sl]
            sl_range = f"{min(sls):g} .. {max(sls):g}" if len(sls) == 2 else (f"{sls[0]:g}" if sls else "?")
            lines.append(
                f"S{se.number} | {len(se.slices)} | I{se.slices[0].index:04d}-I{se.slices[-1].index:04d} | {plane} | {sl_range}"
            )
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
            raise KneeError(f"{label} is still being masked (the first view of an image can take up to a minute "
                            "on this server); call again in about a minute")
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
            caption += f"; {pending} still being masked (tiles say 'masking...'); call get_contact_sheet again in a minute"
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
        caption = _caption(st, se, sl, ov) + f" | size {img.width}x{img.height}"
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
        for sl, (state, val) in zip(chosen, self._gather([s.file for s in chosen])):
            label = f"I{sl.index:04d}"
            if state == "ok":
                img, ov = val  # type: ignore[misc]
                png = imaging.to_png(imaging.apply_crop(img, crop), self.cfg.max_png_bytes)
                out.append(Rendered(png, _caption(st, se, sl, ov)))
            elif state == "pending":
                pending.append(label)
            elif state == "withheld":
                out.append(Rendered(b"", f"{st.id} S{se.number} {label}: withheld (redaction check failed)"))
            else:
                out.append(Rendered(b"", f"{st.id} S{se.number} {label}: {val}"))
        if pending:
            out.append(Rendered(b"", f"still being masked: {', '.join(pending)}. The first view of an image can take "
                                     "up to a minute on this server; call get_slices again for these in about a minute."))
        return out

    def compare(self, a: dict, b: dict) -> Rendered:
        refs = []
        for ref in (a, b):
            st, se = self._resolve(ref["study"], ref["series"])
            refs.append((st, se, se.get(int(ref["index"])), ref.get("crop")))
        results = self._gather([sl.file for _, _, sl, _ in refs])
        pending = [f"{st.date} I{sl.index:04d}" for (st, _, sl, _), (state, _) in zip(refs, results) if state == "pending"]
        if pending:
            raise KneeError(f"still being masked: {', '.join(pending)}; call compare again in about a minute")
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
        """Pre-redact everything (CLI `--warm`). Returns (ok, withheld)."""
        files = [s.file for st in self.catalog.index(refresh=True).studies for se in st.series.values() for s in se.slices]
        res = list(self.pool.map(self._try_redacted, files))
        return sum(r is not None for r in res), sum(r is None for r in res)
