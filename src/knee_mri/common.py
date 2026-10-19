from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from typing import Any

class SafeError(Exception):
    """Only fixed, non-identifying codes/messages may cross the tool boundary."""

SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")

def identifier(value: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise SafeError("INVALID_IDENTIFIER")
    return value

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def safe_path(root: Path, relative: str) -> Path:
    """No absolute paths, traversal, symlinks, or files outside the dataset."""
    p = Path(relative)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise SafeError("INVALID_DATASET_PATH")
    root = root.resolve()
    current = root
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise SafeError("SYMLINK_NOT_ALLOWED")
    resolved = current.resolve()
    if not resolved.is_relative_to(root):
        raise SafeError("INVALID_DATASET_PATH")
    return resolved

def json_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)

def json_read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def finite_float(value: Any) -> float | None:
    import math
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None
