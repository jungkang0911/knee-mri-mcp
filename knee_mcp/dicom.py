"""DICOM support: index by header tags instead of file names, and render pixels for redaction.

Only non-identifying tags ever leave this module: StudyDate, Modality, SeriesNumber, InstanceNumber,
a sanitized SeriesDescription, SliceLocation, SliceThickness, image orientation and frame count.
Patient / physician / institution tags are never read into any output.
"""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass

from PIL import Image

from .drive import DriveFile

HEAD_BYTES = 256 * 1024
_MODALITY = {"MR": "MRI", "CR": "X-ray", "DX": "X-ray", "DR": "X-ray", "RG": "X-ray", "CT": "CT"}
_DESC_OK = re.compile(r"[^A-Za-z0-9 +\-_./()]")


@dataclass
class DicomMeta:
    study_date: str  # YYYY-MM-DD
    modality: str  # "MRI" / "X-ray" / "CT" / ""
    series: int
    instance: int
    description: str  # sanitized SeriesDescription, e.g. "COR PD FS"
    plane: str  # from ImageOrientationPatient: "COR" / "SAG" / "Axial" / ""
    slice_location: str
    slice_thickness: str
    frames: int

    def to_json(self) -> dict:
        return asdict(self)


def is_dicom_candidate(f: DriveFile) -> bool:
    name = f.name.lower()
    if name == "dicomdir":
        return False
    if name.endswith((".dcm", ".dicom")) or f.mime == "application/dicom":
        return True
    return "." not in f.name and f.mime in ("", "application/octet-stream")


def is_dicom_bytes(data: bytes) -> bool:
    return len(data) >= 132 and data[128:132] == b"DICM"


def _num(value) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return ""


def _plane(orientation) -> str:
    try:
        r = [float(v) for v in orientation[:3]]
        c = [float(v) for v in orientation[3:6]]
    except (TypeError, ValueError, IndexError):
        return ""
    n = [abs(r[1] * c[2] - r[2] * c[1]), abs(r[2] * c[0] - r[0] * c[2]), abs(r[0] * c[1] - r[1] * c[0])]
    return ("SAG", "COR", "Axial")[n.index(max(n))]


def _description(text: str, pii_terms: list[str]) -> str:
    desc = re.sub(r"\s+", " ", _DESC_OK.sub(" ", str(text or ""))).strip()[:40]
    low = desc.lower()
    if any(t and t.lower() in low for t in pii_terms):
        return ""
    return desc


def read_meta(data: bytes, pii_terms: list[str] | None = None) -> DicomMeta | None:
    """Header fields from the start of a DICOM file; None if it is not DICOM or the header is incomplete."""
    import pydicom

    if not is_dicom_bytes(data):
        return None
    try:
        ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True)
        raw_date = str(ds.get("StudyDate", "") or ds.get("SeriesDate", "") or "")
        series = int(ds.get("SeriesNumber"))
        instance = int(ds.get("InstanceNumber"))
    except Exception:  # noqa: BLE001 - truncated head or malformed file
        return None
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", raw_date.strip())
    if not m:
        return None
    return DicomMeta(
        study_date=f"{m[1]}-{m[2]}-{m[3]}",
        modality=_MODALITY.get(str(ds.get("Modality", "")).upper(), ""),
        series=series,
        instance=instance,
        description=_description(ds.get("SeriesDescription", ""), pii_terms or []),
        plane=_plane(ds.get("ImageOrientationPatient")),
        slice_location=_num(ds.get("SliceLocation")),
        slice_thickness=_num(ds.get("SliceThickness")),
        frames=int(ds.get("NumberOfFrames", 1) or 1),
    )


def to_image(data: bytes) -> Image.Image:
    """Pixel data as an 8-bit image, with the modality and VOI (window) LUTs applied."""
    import numpy as np
    import pydicom
    from pydicom.pixels import apply_modality_lut, apply_voi_lut

    ds = pydicom.dcmread(io.BytesIO(data))
    arr = ds.pixel_array
    if int(ds.get("NumberOfFrames", 1) or 1) > 1:
        arr = arr[0]
    photometric = str(ds.get("PhotometricInterpretation", "MONOCHROME2"))
    if photometric.startswith("MONOCHROME"):
        arr = apply_voi_lut(apply_modality_lut(arr, ds), ds).astype("float64")
        lo, hi = float(arr.min()), float(arr.max())
        arr = (arr - lo) / (hi - lo) * 255 if hi > lo else arr * 0
        if photometric == "MONOCHROME1":
            arr = 255 - arr
        return Image.fromarray(arr.clip(0, 255).astype(np.uint8), mode="L")
    if photometric in ("RGB", "YBR_FULL", "YBR_FULL_422"):
        from pydicom.pixels import convert_color_space

        if photometric != "RGB":
            arr = convert_color_space(arr, photometric, "RGB")
        return Image.fromarray(arr.astype(np.uint8), mode="RGB")
    raise ValueError("unsupported photometric interpretation")
