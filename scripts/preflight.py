#!/usr/bin/env python3
from importlib import util,metadata
import json,shutil
results={}
for p in ('mcp','pydicom','PIL','numpy','scipy','pytesseract','yaml','uvicorn','httpx'):
    results[p]=bool(util.find_spec(p))
for exe in ('tesseract','npx','docker'):results[exe]=bool(shutil.which(exe))
print(json.dumps(results,indent=2))
