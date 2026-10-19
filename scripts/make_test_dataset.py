#!/usr/bin/env python3
"""Create non-clinical synthetic data for transport tests. Not an MRI anonymization shortcut."""
import argparse,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
import conftest

p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=Path('synthetic-run'))
a=p.parse_args()
if a.out.exists():raise SystemExit('OUTPUT_EXISTS')
a.out.mkdir(parents=True)
# Invoke the synthetic-only fixture factory, not any clinical reviewer bypass.
root=conftest.demo_dataset.__wrapped__(a.out)
print(str(root))
