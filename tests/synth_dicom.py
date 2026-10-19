"""Synthetic DICOM files: fake patient tags plus fake PII burned into the pixels (no real data)."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image
from synth import knee_image

FAKE_TAGS = {
    "PatientName": "WANG^XIAOMING",
    "PatientID": "20481736",
    "PatientBirthDate": "19750412",
    "ReferringPhysicianName": "CHEN^KUOHUA",
    "InstitutionName": "SAMPLE GENERAL HOSPITAL",
}
FAKE_TAG_TOKENS = ["WANG", "XIAOMING", "20481736", "19750412", "CHEN", "KUOHUA", "SAMPLE GENERAL"]


def dicom_bytes(
    instance: int,
    series: int = 9,
    study_date: str = "20190320",
    description: str = "COR PD FS",
    orientation=(1, 0, 0, 0, 0, -1),  # coronal
    slice_location: float | None = None,
    photometric: str = "MONOCHROME2",
    burned_pii: bool = True,
) -> bytes:
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

    gray = Image.open(io.BytesIO(knee_image(instance, plane="", sl="", st="", pii=burned_pii))).convert("L")
    arr = (np.asarray(gray, dtype=np.uint16) * 16).astype(np.uint16)  # 12-bit range
    if photometric == "MONOCHROME1":
        arr = (4095 - arr).astype(np.uint16)

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    for k, v in FAKE_TAGS.items():
        setattr(ds, k, v)
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = "1.2.3.4"
    ds.SeriesInstanceUID = f"1.2.3.4.{series}"
    ds.Modality = "MR"
    ds.StudyDate = study_date
    ds.SeriesNumber = series
    ds.InstanceNumber = instance
    ds.SeriesDescription = description
    ds.ImageOrientationPatient = list(orientation)
    ds.SliceLocation = -20.0 + 4 * instance if slice_location is None else slice_location
    ds.SliceThickness = 3.0
    ds.Rows, ds.Columns = arr.shape
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = photometric
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0
    ds.PixelData = arr.tobytes()
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()
