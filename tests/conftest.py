from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from synth import knee_image  # noqa: E402

from knee_mcp.config import Config  # noqa: E402
from knee_mcp.drive import DriveFile  # noqa: E402
from knee_mcp.server import build_server  # noqa: E402
from knee_mcp.service import KneeService  # noqa: E402

MTIME = "2025-01-01T00:00:00.000Z"


class MockDrive:
    """Stands in for Google Drive: a fixed listing (deliberately shuffled) and synthetic JPEGs."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[DriveFile, bytes]] = {}
        self.downloads = 0
        n = 0

        def add(name: str, folders: tuple[str, ...], data: bytes, mtime: str = MTIME) -> None:
            nonlocal n
            n += 1
            fid = f"f{n:04d}"
            self.files[fid] = (DriveFile(fid, name, folders, mtime), data)

        # 2019 TSGH MRI, series 7: 20 images with fake PII burned in.
        for i in range(1, 21):
            add(f"2019-03-20_TSGH_RK_S7_I{i:04d}.jpg", ("2019-03-20_TSGH_MRI",),
                knee_image(i, plane="COR PD", sl=f"{-30 + 3 * i:.1f}", st="3.0"))
        # 2024 VGHTPE MRI: series 4 split across "S4" and "S04" names.
        for i in range(1, 4):
            add(f"2024-12-03_VGHTPE_RK_S4_I{i:04d}.jpg", ("2024-12-03_VGHTPE_MRI",),
                knee_image(100 + i, plane="SAG PD", pii=False))
        for i in range(4, 6):
            add(f"2024-12-03_VGHTPE_RK_S04_I{i:04d}.jpg", ("2024-12-03_VGHTPE_MRI",),
                knee_image(100 + i, plane="SAG PD", pii=False))
        add("2024-12-03_VGHTPE_RK_S5_I0099.jpg", ("2024-12-03_VGHTPE_MRI",), knee_image(199, plane="AX T2 FS", pii=False))
        # 2018 X-ray with names that break the rule -> folder inference + issues.
        for i in range(1, 3):
            add(f"img-00001-{i:05d}.jpg", ("2018-12-31_TSGH_XR", "S1"), knee_image(200 + i, plane="", pii=False))
        add("notes.txt", (), b"not an image")
        self._order = list(self.files)
        random.Random(7).shuffle(self._order)

    def list_files(self) -> list[DriveFile]:
        return [self.files[k][0] for k in self._order if self.files[k][0].name.lower().endswith((".jpg", ".png"))]

    def download(self, file: DriveFile) -> bytes:
        self.downloads += 1
        return self.files[file.id][1]


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(cache_dir=tmp_path / "cache", ocr_langs="eng", workers=4)


@pytest.fixture
def drive() -> MockDrive:
    return MockDrive()


@pytest.fixture
def service(cfg: Config, drive: MockDrive) -> KneeService:
    return KneeService(cfg, drive)


@pytest.fixture
def server(cfg: Config, service: KneeService):
    return build_server(cfg, service)
