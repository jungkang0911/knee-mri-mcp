"""Owner review of images where OCR found possible personal data inside the image area (not masked).

    python -m knee_mcp.review list
    python -m knee_mcp.review approve 2019-03-20_TSGH S10 10     # show as is (it was not personal data)
    python -m knee_mcp.review mask    2019-03-20_TSGH S10 10     # black out the flagged boxes, then show

Look at the image first with `python -m knee_mcp.prebuild --debug-overlays DIR` (flagged boxes in yellow).
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .server import build_service, setup

    parser = argparse.ArgumentParser(prog="python -m knee_mcp.review")
    parser.add_argument("action", choices=["list", "approve", "mask"])
    parser.add_argument("study", nargs="?")
    parser.add_argument("series", nargs="?")
    parser.add_argument("index", nargs="?", type=int)
    args = parser.parse_args(argv)
    service = build_service(setup())
    idx = service.catalog.index()
    if args.action == "list":
        n = 0
        for st in idx.studies:
            for se in st.series.values():
                for sl in se.slices:
                    if service.proc.status(sl.file) == "review":
                        n += 1
                        boxes = service.proc.info(sl.file).get("review_boxes", [])
                        print(f"{st.id} S{se.number} {sl.index}: {len(boxes)} box(es) {boxes}")
        print(f"{n} image(s) waiting for review")
        return 0
    if not (args.study and args.series and args.index is not None):
        parser.error("approve/mask need: study series index")
    se = idx.get_study(args.study).get_series(args.series)
    service.proc.decide_review(se.get(args.index).file, args.action)
    print(f"{args.action}d {args.study} {args.series} I{args.index:04d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
