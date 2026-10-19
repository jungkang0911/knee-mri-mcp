"""Stable, descriptive names for derived right-knee images; never rename raw files."""
from __future__ import annotations
from .common import SafeError, identifier
from .metadata import technical_description

NAMING_VERSION = "descriptive-v1"


def series_stem(study_id: str, series_no: int, series_desc: str) -> str:
    """Use the configured study and verified technical metadata, not diagnostic labels."""
    identifier(study_id)
    if type(series_no) is not int or series_no < 0 or series_no > 999999:
        raise SafeError("INVALID_SERIES")
    if not isinstance(series_desc, str) or not series_desc:
        raise SafeError("INVALID_SERIES_DESCRIPTION")
    # Raw OCR/DICOM text and speculative disease labels may not enter filenames.
    identifier(series_desc)
    if technical_description(series_desc)["series_desc"] != series_desc:
        raise SafeError("INVALID_SERIES_DESCRIPTION")
    return f"{study_id}_RK_S{series_no:02d}_{series_desc}"


def slice_filename(study_id: str, series_no: int, series_desc: str, index: int) -> str:
    """I is the verified source index, not a new sort-order number."""
    if type(index) is not int or index < 1 or index > 9999999:
        raise SafeError("INVALID_SLICE_INDEX")
    return f"{series_stem(study_id, series_no, series_desc)}_I{index:04d}.png"


def contact_sheet_filename(study_id: str, series_no: int, series_desc: str) -> str:
    return f"{series_stem(study_id, series_no, series_desc)}_CONTACT.png"
