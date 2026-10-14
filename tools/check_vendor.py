#!/usr/bin/env python3
"""The pinned scripts in tools/vendor/ are exactly the bytes named by VENDORED.json."""
import hashlib
import json
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parent / "vendor"
doc = json.loads((VENDOR / "VENDORED.json").read_text(encoding="utf-8"))
bad = [name for name, digest in doc["files"].items()
       if hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() != digest]
extra = sorted(p.name for p in VENDOR.iterdir() if p.is_file() and p.name != "VENDORED.json" and p.name not in doc["files"])
if bad or extra:
    print(f"vendored files differ from VENDORED.json: changed {bad}, unlisted {extra}")
    sys.exit(1)
print(f"vendor ok: {', '.join(doc['files'])} from {doc['source']['commit'][:12]}")
