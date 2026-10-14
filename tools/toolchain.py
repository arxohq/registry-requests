#!/usr/bin/env python3
"""Fetch the pinned `law` binary (toolchain.lock.json) and verify it before use.

    python3 tools/toolchain.py install --out <new dir>     # prints the binary path

Standard library only. Nothing from the tarball is executed during installation;
the tarball and the binary are both checked against the lock.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_TARBALL = 128 * 1024 * 1024


def platform_key() -> str:
    system, machine = platform.system(), platform.machine().lower()
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux-x64"
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        return "darwin-arm64"
    raise SystemExit(f"no pinned law binary for {system} {machine}")


def install(out: Path, lock_path: Path) -> Path:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("format") != "arxo.registry-intake-toolchain/1":
        raise SystemExit(f"{lock_path}: unsupported lock format")
    pin = lock["platforms"][platform_key()]
    if out.exists():
        raise SystemExit(f"{out}: the installation directory must be new")
    with urllib.request.urlopen(pin["tarball"], timeout=300) as response:
        data = response.read(MAX_TARBALL + 1)
    if len(data) > MAX_TARBALL or hashlib.sha256(data).hexdigest() != pin["tarballSha256"]:
        raise SystemExit(f"{pin['tarball']}: tarball sha256 does not match the lock")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember("package/law")
        if not member.isreg():
            raise SystemExit("package/law is not a regular file")
        binary = archive.extractfile(member).read()
    if hashlib.sha256(binary).hexdigest() != pin["binarySha256"]:
        raise SystemExit("law binary sha256 does not match the lock")
    out.mkdir(parents=True)
    target = out / "law"
    target.write_bytes(binary)
    target.chmod(0o755)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    i = sub.add_parser("install")
    i.add_argument("--out", type=Path, required=True)
    i.add_argument("--lock", type=Path, default=ROOT / "toolchain.lock.json")
    args = parser.parse_args()
    print(install(args.out, args.lock))
    return 0


if __name__ == "__main__":
    sys.exit(main())
