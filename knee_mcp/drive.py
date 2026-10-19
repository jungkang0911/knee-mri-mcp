"""Google Drive access (drive.readonly, restricted to one root folder) plus a local download cache."""

from __future__ import annotations

import io
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .errors import KneeError

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    folders: tuple[str, ...]  # folder names from the root folder down (root itself excluded)
    modified_time: str


class DriveSource(Protocol):
    def list_files(self) -> list[DriveFile]: ...

    def download(self, file: DriveFile) -> bytes: ...


class GoogleDriveSource:
    """Walks the tree under `root_folder_id` only; never queries or downloads anything outside it."""

    def __init__(self, root_folder_id: str, credentials) -> None:
        from googleapiclient.discovery import build

        if not root_folder_id:
            raise KneeError("KNEE_ROOT_FOLDER_ID is not set")
        self.root = root_folder_id
        self._creds = credentials
        self._build = build
        self._local = threading.local()
        self._allowed: set[str] = set()

    def _svc(self):
        # googleapiclient service objects are not thread-safe; keep one per thread.
        svc = getattr(self._local, "svc", None)
        if svc is None:
            svc = self._build("drive", "v3", credentials=self._creds, cache_discovery=False)
            self._local.svc = svc
        return svc

    def _children(self, folder_id: str) -> list[dict]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id):
            raise KneeError("invalid Drive folder id")
        out: list[dict] = []
        token = None
        while True:
            resp = (
                self._svc()
                .files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                    pageSize=1000,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            out.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def list_files(self) -> list[DriveFile]:
        files: list[DriveFile] = []
        stack: list[tuple[str, tuple[str, ...]]] = [(self.root, ())]
        seen = {self.root}
        while stack:
            folder_id, path = stack.pop()
            for item in self._children(folder_id):
                if item["mimeType"] == FOLDER_MIME:
                    if item["id"] not in seen and len(path) < 8:
                        seen.add(item["id"])
                        stack.append((item["id"], path + (item["name"],)))
                elif item["mimeType"].startswith("image/"):
                    files.append(DriveFile(item["id"], item["name"], path, item["modifiedTime"]))
        self._allowed = {f.id for f in files}
        return files

    def download(self, file: DriveFile) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        if file.id not in self._allowed:
            raise KneeError("file is not inside the configured root folder")
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, self._svc().files().get_media(fileId=file.id))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()


def load_google_credentials(client_secrets: Path | None, token_file: Path | None):
    """Service account (GOOGLE_SERVICE_ACCOUNT_FILE) or a stored user token from `python -m knee_mcp.google_login`."""
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if sa:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(sa, scopes=SCOPES)

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_file or not token_file.exists():
        raise KneeError("Google Drive is not authorized yet; run `python -m knee_mcp.google_login`")
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if set(creds.scopes or SCOPES) - set(SCOPES):
        raise KneeError("stored Google token has scopes beyond drive.readonly; re-run google_login")
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


class FileCache:
    """Raw Drive bytes on disk, keyed by fileId + modifiedTime. These files DO contain PII: keep the dir private."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "drive"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _key(self, file: DriveFile) -> str:
        return f"{_safe(file.id)}_{_safe(file.modified_time)}"

    def get(self, source: DriveSource, file: DriveFile) -> bytes:
        path = self.dir / f"{self._key(file)}.bin"
        if path.exists():
            return path.read_bytes()
        data = source.download(file)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        with self._lock:  # drop older versions of the same file
            for old in self.dir.glob(f"{_safe(file.id)}_*.bin"):
                if old != path:
                    old.unlink(missing_ok=True)
        return data


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", text)


class LocalFolderSource:
    """Development stand-in for Drive (KNEE_LOCAL_DIR): same layout, read from a local folder."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def list_files(self) -> list[DriveFile]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"):
                rel = p.relative_to(self.root)
                mtime = f"{p.stat().st_mtime_ns}"
                out.append(DriveFile(rel.as_posix(), p.name, tuple(rel.parts[:-1]), mtime))
        return out

    def download(self, file: DriveFile) -> bytes:
        path = (self.root / file.id).resolve()
        if self.root not in path.parents:
            raise KneeError("file is not inside the configured root folder")
        return path.read_bytes()
