"""Offline preprocessing: mask every image ahead of time and write <cache>/manifest.json.

    python -m knee_mcp.prebuild

Incremental (unchanged fileId + modifiedTime are skipped), so it is safe to re-run after uploading new
studies. MCP tools then only read the manifest and the masked PNGs.
"""

from __future__ import annotations

import sys
import time


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    from .server import build_service, setup

    parser = argparse.ArgumentParser(prog="python -m knee_mcp.prebuild")
    parser.add_argument("--debug-overlays", metavar="DIR", help="also write every masked image with its "
                        "blacked-out boxes outlined (red) and review boxes (yellow) to DIR")
    args = parser.parse_args(argv)
    cfg = setup()
    service = build_service(cfg)
    t0 = time.time()
    manifest = service.prebuild(
        progress=lambda m: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True),
        debug_dir=Path(args.debug_overlays) if args.debug_overlays else None,
    )
    ok = sum(e["redaction_status"] == "ok" for e in manifest)
    withheld = sum(e["redaction_status"] == "withheld" for e in manifest)
    review = sum(e["redaction_status"] == "review" for e in manifest)
    print(f"done: {ok} masked, {review} waiting for review, {withheld} withheld, "
          f"{len(manifest) - ok - withheld - review} not processed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
