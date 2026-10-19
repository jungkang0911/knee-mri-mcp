"""Offline preprocessing: mask every image ahead of time and write <cache>/manifest.json.

    python -m knee_mcp.prebuild

Incremental (unchanged fileId + modifiedTime are skipped), so it is safe to re-run after uploading new
studies. MCP tools then only read the manifest and the masked PNGs.
"""

from __future__ import annotations

import sys
import time


def main() -> int:
    from .server import build_service, setup

    cfg = setup()
    service = build_service(cfg)
    t0 = time.time()
    manifest = service.prebuild(progress=lambda m: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True))
    ok = sum(e["redaction_status"] == "ok" for e in manifest)
    withheld = sum(e["redaction_status"] == "withheld" for e in manifest)
    print(f"done: {ok} masked, {withheld} withheld, {len(manifest) - ok - withheld} not processed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
