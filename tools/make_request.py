#!/usr/bin/env python3
"""Turn a `law publish --candidate` manifest into a registry request.

For @arxo/cli 0.2.6, which has `--candidate` but not yet `--request`:

    law publish <artifact> --candidate cand.json --bytes bytes/
    python3 make_request.py cand.json --source acme/parcel-tariff@parcel-tariff-0.1.0 \\
        --commit <sha> --license Apache-2.0 --out requests/acme.parcel_tariff-0.1.0/request.json

From the next CLI release the same file comes from
`law publish <artifact> --request <file> --bytes <dir> --source … --commit … --license …`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--source", required=True, help="owner/repo@tag of the Release with the bytes")
    parser.add_argument("--commit", required=True, help="the commit the tag points to")
    parser.add_argument("--license", required=True, help="SPDX identifier")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    if candidate.get("schema") != "arxo.registry-candidate/1":
        raise SystemExit(f"{args.candidate}: not a law publish --candidate manifest")
    repository, sep, release = args.source.partition("@")
    if not sep:
        raise SystemExit("--source expects owner/repo@tag")
    request = {
        "schema": "arxo.registry-request/1",
        "source": {"repository": repository, "release": release, "commit": args.commit},
        "license": args.license,
        "packages": candidate["packages"],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
