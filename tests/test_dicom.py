"""DICOM: indexed by header tags (no renaming needed), tags never leak, burned-in text still masked."""

from __future__ import annotations

import base64
import io
import random

import pytest
from mcp import Client
from PIL import Image
from synth import FAKE_PII_TOKENS
from synth_dicom import FAKE_TAG_TOKENS, dicom_bytes
from test_tools import _ocr_all, _png

from knee_mcp import imaging
from knee_mcp.config import Config
from knee_mcp.dicom import DicomMeta, is_dicom_candidate, read_meta, to_image
from knee_mcp.drive import DriveFile
from knee_mcp.server import build_server
from knee_mcp.service import KneeService

ALL_PII = [t.upper() for t in FAKE_PII_TOKENS + FAKE_TAG_TOKENS]


class DicomDrive:
    """A hospital-CD style folder: extensionless DICOM files whose names say nothing about order."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[DriveFile, bytes]] = {}
        self.downloads = 0
        self.head_reads = 0
        folder = ("2019-03-20_TSGH_光碟", "DICOM", "PA000000", "ST000000", "SE000009")
        instances = list(range(1, 7))
        random.Random(3).shuffle(instances)  # file IM000001 is not instance 1
        for n, inst in enumerate(instances, start=1):
            self._add(f"IM{n:06d}", folder, dicom_bytes(inst), "application/octet-stream")
        self._add("IM000001.dcm", ("2024-12-03_VGHTPE_DICOM",),
                  dicom_bytes(1, series=3, study_date="20241203", description="SAG PD", orientation=(0, 1, 0, 0, 0, -1),
                              photometric="MONOCHROME1", burned_pii=False), "application/dicom")
        self._add("DICOMDIR", ("2019-03-20_TSGH_光碟",), b"\0" * 200, "application/octet-stream")
        self._add("README", ("2019-03-20_TSGH_光碟",), b"not dicom at all", "application/octet-stream")

    def _add(self, name, folders, data, mime):
        fid = f"d{len(self.files):03d}"
        self.files[fid] = (DriveFile(fid, name, folders, "2025-01-01T00:00:00Z", mime), data)

    def list_files(self):
        return [f for f, _ in self.files.values() if f.name != "DICOMDIR"]

    def download_head(self, file, n):
        self.head_reads += 1
        return self.files[file.id][1][:n]

    def download(self, file):
        self.downloads += 1
        return self.files[file.id][1]


@pytest.fixture
def dcm_drive():
    return DicomDrive()


@pytest.fixture
def dcm_server(tmp_path, dcm_drive):
    cfg = Config(cache_dir=tmp_path / "cache", ocr_langs="eng", workers=2, tool_budget=600)
    return build_server(cfg, KneeService(cfg, dcm_drive))


def _texts(result) -> str:
    return " ".join(c.text for c in result.content if c.type == "text").upper()


def test_read_meta_and_candidates():
    meta = read_meta(dicom_bytes(4))
    assert meta == DicomMeta("2019-03-20", "MRI", 9, 4, "COR PD FS", "COR", "-4", "3", 1)
    assert read_meta(b"\0" * 300) is None
    assert read_meta(dicom_bytes(4)[:200]) is None  # truncated header -> caller falls back to full download
    f = lambda name, mime="": DriveFile("x", name, (), "t", mime)  # noqa: E731
    assert is_dicom_candidate(f("IM000001", "application/octet-stream"))
    assert is_dicom_candidate(f("scan.dcm")) and is_dicom_candidate(f("x.bin", "application/dicom"))
    assert not is_dicom_candidate(f("DICOMDIR")) and not is_dicom_candidate(f("a.jpg", "image/jpeg"))


def test_monochrome1_is_inverted():
    m2 = to_image(dicom_bytes(2, burned_pii=False))
    m1 = to_image(dicom_bytes(2, burned_pii=False, photometric="MONOCHROME1"))
    assert abs(m1.getpixel((256, 150)) - m2.getpixel((256, 150))) <= 2  # same appearance after inversion


async def test_list_studies_from_tags_without_full_downloads(dcm_server, dcm_drive):
    async with Client(dcm_server) as c:
        r = await c.call_tool("list_studies", {})
    text = r.content[0].text
    assert "2019-03-20_TSGH | 2019-03-20 | TSGH | MRI | 1 | 6" in text
    assert "2024-12-03_VGHTPE | 2024-12-03 | VGHTPE | MRI | 1 | 1" in text
    assert "skipped README: not a readable DICOM file" in text
    assert "DICOMDIR" not in text
    assert dcm_drive.downloads == 0 and dcm_drive.head_reads == 8  # headers only, via HTTP Range
    for token in ALL_PII:
        assert token not in text.upper()


async def test_list_series_uses_tags(dcm_server):
    async with Client(dcm_server) as c:
        r = await c.call_tool("list_series", {"study": "2019-03-20"})
        r2 = await c.call_tool("list_series", {"study": "2024-12-03"})
    row = [ln for ln in r.content[0].text.splitlines() if ln.startswith("S9 ")][0]
    assert "| 6 |" in row and "I0001-I0006" in row and "COR PD FS" in row and "-16 .. 4" in row
    assert "SAG PD" in r2.content[0].text


async def test_get_slice_dicom(dcm_server):
    async with Client(dcm_server) as c:
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "S09", "index": 5})
    assert not r.is_error, r.content
    assert [x.type for x in r.content] == ["image", "text"]
    img = _png(r.content[0])
    caption = _texts(r)
    assert "2019-03-20_TSGH S9 I0005" in caption and "COR PD FS" in caption and "SL 0" in caption and "ST 3" in caption
    ocr = _ocr_all(img)
    for token in ALL_PII:
        assert token not in caption and token not in ocr, token


async def test_dicom_contact_sheet_order_by_instance_number(dcm_server, monkeypatch):
    seen = {}
    real = imaging.contact_sheet

    def spy(tiles, cols, thumb=180):
        seen["labels"] = [label for label, _ in tiles]
        seen["withheld"] = sum(img is None for _, img in tiles)
        return real(tiles, cols, thumb)

    monkeypatch.setattr(imaging, "contact_sheet", spy)
    async with Client(dcm_server) as c:
        r = await c.call_tool("get_contact_sheet", {"study": "2019-03-20", "series": "9", "cols": 3})
    assert not r.is_error
    assert seen["labels"] == [f"I{i:04d}" for i in range(1, 7)] and seen["withheld"] == 0
    for token in ALL_PII:
        assert token not in _ocr_all(_png(r.content[0]))


async def test_dicom_originals_not_written_to_disk(dcm_server, tmp_path):
    async with Client(dcm_server) as c:
        await c.call_tool("get_slice", {"study": "2024-12-03", "series": "3", "index": 1})
    for p in (tmp_path / "cache").rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            assert b"DICM" not in data[:200] and b"XIAOMING" not in data and b"20481736" not in data, p.name


async def test_undecodable_dicom_gives_safe_error(dcm_server, monkeypatch):
    import knee_mcp.dicom as d

    def broken(data):
        raise RuntimeError(r"C:\secret\WANG^XIAOMING pixel data")

    monkeypatch.setattr(d, "to_image", broken)
    async with Client(dcm_server) as c:
        r = await c.call_tool("get_slice", {"study": "2019-03-20", "series": "9", "index": 2})
    assert r.is_error and "cannot decode this DICOM image" in r.content[0].text
    assert "WANG" not in r.content[0].text and "\\" not in r.content[0].text
