#!/usr/bin/env python3
"""Publish only the packaged code allowlist to a NEW PRIVATE GitHub repository.

Requires git and GitHub CLI authenticated with `gh auth login`.
Never reads raw/, dataset/ or OCR caches. Refuses modified release files,
symlinks and an existing target repository. No tokens are read or printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile


def run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def require_ok(result: subprocess.CompletedProcess[str], action: str) -> str:
    if result.returncode != 0:
        # Do not print environment values, credentials or local filesystem paths.
        raise RuntimeError(action + " failed. No credentials were printed.")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="jungkang0911/knee-mri-mcp")
    parser.add_argument("--check-only", action="store_true",
                        help="Validate upload files without GitHub access or writes.")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", args.repo):
        raise ValueError("Repository must be owner/name.")

    root = Path(__file__).resolve().parents[1]
    manifest_path = root / "github-upload-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checked: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    banned_dirs = {"raw", "dataset", ".private", ".secrets", ".git", ".venv", "__pycache__"}
    banned_exts = {".png", ".jpg", ".jpeg", ".dcm", ".tif", ".tiff",
                   ".npy", ".npz", ".zip", ".pem", ".key"}
    for record in manifest["files"]:
        relative = PurePosixPath(record["path"])
        if (relative.is_absolute() or ".." in relative.parts or "\\" in str(relative)
                or ":" in str(relative) or str(relative) in seen):
            raise ValueError("Unsafe or repeated manifest path.")
        if (any(part in banned_dirs or part.startswith("dataset") for part in relative.parts[:-1])
                or relative.suffix.lower() in banned_exts
                or (relative.name.startswith(".env") and relative.name != ".env.example")):
            raise ValueError("Upload manifest contains an excluded file type.")
        source = root.joinpath(*relative.parts)
        if any(p.is_symlink() for p in [source, *source.parents] if p != root):
            raise ValueError("Symlinks are not allowed.")
        data = source.read_bytes()
        data.decode("utf-8")  # Code-and-documents package only.
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("A packaged file changed; publication stopped for review.")
        seen.add(str(relative))
        checked.append((str(relative), data))
    print(f"Validated {len(checked)} code/document files; no MRI image files selected.")
    if args.check_only:
        print("CHECK ONLY: no repository created, no files pushed.")
        return 0

    for tool in ("git", "gh"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Install {tool} before publishing.")
    identity = json.loads(require_ok(run(["gh", "api", "--hostname", "github.com", "user"]),
                                    "GitHub authentication (run gh auth login)"))
    owner = args.repo.split("/", 1)[0]
    if identity.get("login", "").lower() != owner.lower():
        raise RuntimeError("Authenticated account does not match the requested owner.")
    full_repo = "github.com/" + args.repo
    exists = run(["gh", "repo", "view", full_repo, "--json", "nameWithOwner,isPrivate"])
    if exists.returncode == 0:
        raise RuntimeError("Target repository already exists. It was NOT modified.")

    with tempfile.TemporaryDirectory(prefix="knee-mri-code-publish-") as folder:
        work = Path(folder)
        for name, data in checked:
            destination = work / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        (work / "github-upload-manifest.json").write_bytes(manifest_path.read_bytes())
        require_ok(run(["git", "init", "-b", "main"], cwd=work), "Git initialization")
        require_ok(run(["git", "add", "--all"], cwd=work), "Code staging")
        author = identity["login"]
        email = f'{identity["id"]}+{author}@users.noreply.github.com'
        require_ok(run(["git", "-c", f"user.name={author}", "-c", f"user.email={email}",
                        "commit", "-m", "Initial MRI dataset and private MCP server code"],
                       cwd=work), "Initial commit")
        result = run(["gh", "repo", "create", full_repo, "--private",
                      "--source", str(work), "--remote", "origin", "--push",
                      "--description", "MRI dataset curation and read-only MCP ImageContent server"],
                     cwd=work)
        if result.returncode:
            raise RuntimeError("GitHub creation/push failed. A new empty repository may "
                               "have been created; check GitHub before retrying. "
                               "Existing repositories were not overwritten or deleted.")
        remote = json.loads(require_ok(
            run(["gh", "repo", "view", full_repo, "--json", "url,isPrivate"]),
            "Repository readback"))
        if not remote.get("isPrivate"):
            raise RuntimeError("Private visibility could not be verified.")
        local_sha = require_ok(run(["git", "rev-parse", "HEAD"], cwd=work),
                               "Local commit readback").strip()
        remote_sha = require_ok(run(["gh", "api", "--hostname", "github.com",
                                    f"repos/{args.repo}/git/ref/heads/main", "--jq", ".object.sha"]),
                                "Remote commit verification").strip()
        if local_sha != remote_sha:
            raise RuntimeError("Remote commit did not match the packaged commit.")
        print("PRIVATE REPOSITORY VERIFIED: " + remote["url"])
        print("COMMIT VERIFIED: " + remote_sha)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        if isinstance(exc, OSError):
            print("Publication stopped: local file or process unavailable.", file=sys.stderr)
        else:
            print("Publication stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
