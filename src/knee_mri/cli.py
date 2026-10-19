from __future__ import annotations
import argparse
import json
from pathlib import Path
from .common import SafeError

def main():
    parser=argparse.ArgumentParser(prog="knee-mri")
    sub=parser.add_subparsers(dest="command",required=True)
    p=sub.add_parser("build");p.add_argument("--raw",type=Path,default=Path("raw"));p.add_argument("--out",type=Path,default=Path("dataset"))
    p.add_argument("--config",type=Path,default=Path("config/studies.yaml"));p.add_argument("--private",type=Path,default=Path(".private"))
    p=sub.add_parser("release");p.add_argument("--dataset",type=Path,default=Path("dataset"))
    p=sub.add_parser("verify");p.add_argument("--dataset",type=Path,default=Path("dataset"))
    args=parser.parse_args()
    try:
        if args.command=="build":
            from .build import build
            m=build(args.raw,args.out,args.config,args.private)
            print(json.dumps(m["statistics"],ensure_ascii=False))
            print("NOT_RELEASED: review validation_report.md and every full-resolution PNG, then edit review.json")
            return 2 if m["statistics"]["errors"] else 0
        elif args.command=="release":
            from .dataset import release
            release(args.dataset);print("RELEASED: hashes verified, human review declarations recorded")
        else:
            from .dataset import Dataset
            ds=Dataset(args.dataset);print(f"VERIFIED: {len(ds.slices)} slices")
    except SafeError as e:
        print(str(e));return 2
    except Exception:
        print("OPERATION_FAILED: check local configuration and dependencies");return 2
    return 0
if __name__=="__main__":raise SystemExit(main())
