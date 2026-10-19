"""Tool logic: catalog lookups, cached redaction, and image composition. Returns plain Python values."""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import imaging
from .catalog import Catalog, Series, Slice, Study
from .config import Config
from .drive import DriveFile, DriveSource, FileCache
from .errors import KneeError, RedactionFailed
from .redact import REDACTION_VERSION, Overlay, ocr_words, read_overlay, redact

MAX_SLICES = 8


@dataclass
class Rendered:
    png: bytes
    caption: str


class Processor:
    """Redacted images and overlay info, cached on disk by fileId + modifiedTime + redaction settings."""

    def __init__(self, cfg: Config, source: DriveSource, files: FileCache) -> None:
        self.cfg = cfg
        self.source = source
        self.files = files
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
            img = Image.open(io.BytesIO(self.files.get(self.source, f)))
            ov = read_overlay(ocr_words(img, self.cfg.ocr_langs))
            self._write(meta, json.dumps({"overlay": ov.__dict__, "status": "overlay"}).encode())
            return ov

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
            try:
                img, ov = redact(self.files.get(self.source, f), self.cfg.ocr_langs, self.cfg.pii_terms)
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
        self.catalog = Catalog(source, cfg.study_types, cfg.pii_terms, cfg.listing_ttl)
        self.proc = Processor(cfg, source, FileCache(cfg.cache_dir))
        self.pool = ThreadPoolExecutor(max_workers=max(1, cfg.workers))

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
                lines.append(f"{s.id} | {s.date} | {s.hospital} | {s.kind} | {len(s.series)} | {n}")
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

    def contact_sheet(self, study: str, series: str | int, cols: int = 5) -> Rendered:
        st, se = self._resolve(study, series)
        results = list(self.pool.map(self._try_redacted, [s.file for s in se.slices]))
        tiles = [(f"I{s.index:04d}", img) for s, img in zip(se.slices, results)]
        sheet = imaging.contact_sheet(tiles, cols)
        withheld = sum(1 for _, img in tiles if img is None)
        caption = f"{st.id} S{se.number}: {len(tiles)} images, I{se.slices[0].index:04d}-I{se.slices[-1].index:04d}"
        if withheld:
            caption += f"; {withheld} withheld (redaction check failed)"
        return Rendered(imaging.to_png(sheet, self.cfg.max_png_bytes), caption)

    def _try_redacted(self, f: DriveFile) -> Image.Image | None:
        try:
            return self.proc.redacted(f)[0]
        except RedactionFailed:
            return None

    def slice(self, study: str, series: str | int, index: int, crop=None, zoom: float = 1) -> Rendered:
        st, se = self._resolve(study, series)
        sl = se.get(int(index))
        img, ov = self.proc.redacted(sl.file)
        out = imaging.apply_zoom(imaging.apply_crop(img, crop), zoom)
        caption = _caption(st, se, sl, ov) + f" | size {img.width}x{img.height}"
        if crop:
            caption += f" | crop {list(crop)}"
        return Rendered(imaging.to_png(out, self.cfg.max_png_bytes), caption)

    def slices(self, study: str, series: str | int, start: int, end: int, crop=None) -> list[Rendered]:
        st, se = self._resolve(study, series)
        lo, hi = sorted((int(start), int(end)))
        chosen = [s for s in se.slices if lo <= s.index <= hi]
        if not chosen:
            raise KneeError(f"no images between I{lo:04d} and I{hi:04d} in S{se.number}")
        if len(chosen) > MAX_SLICES:
            raise KneeError(f"range has {len(chosen)} images; at most {MAX_SLICES} per call")

        def one(sl: Slice) -> Rendered:
            img, ov = self.proc.redacted(sl.file)
            return Rendered(imaging.to_png(imaging.apply_crop(img, crop), self.cfg.max_png_bytes), _caption(st, se, sl, ov))

        return list(self.pool.map(one, chosen))

    def compare(self, a: dict, b: dict) -> Rendered:
        imgs, labels = [], []
        for ref in (a, b):
            st, se = self._resolve(ref["study"], ref["series"])
            sl = se.get(int(ref["index"]))
            img, ov = self.proc.redacted(sl.file)
            imgs.append(imaging.apply_crop(img, ref.get("crop")))
            labels.append(f"{st.date} {st.hospital} S{se.number} I{sl.index:04d}" + (f" {ov.plane}" if ov.plane else ""))
        out = imaging.side_by_side(imgs[0], labels[0], imgs[1], labels[1])
        return Rendered(imaging.to_png(out, self.cfg.max_png_bytes), f"left: {labels[0]} | right: {labels[1]}")

    def warm(self) -> tuple[int, int]:
        """Pre-redact everything (CLI `--warm`). Returns (ok, withheld)."""
        files = [s.file for st in self.catalog.index(refresh=True).studies for se in st.series.values() for s in se.slices]
        res = list(self.pool.map(self._try_redacted, files))
        return sum(r is not None for r in res), sum(r is None for r in res)
