#!/usr/bin/env python3
"""Intake of external requests to the public Arxo registry.

Two entry points, one per workflow:

    python3 tools/intake.py check  --base <ref> --law <binary> --work <dir> [--report <file>]
    python3 tools/intake.py accept --before <ref> --law <binary> --work <dir> --registry-remote <url>

`check` runs on a pull request without secrets. It never publishes anything:
it validates the request, downloads the bytes from the author's public Release
anonymously, compares sha256 and size, restores the artifacts, publishes them
into a SCRATCH clone of the public registry with the pinned `law`, runs the
registry's own gate and installs the packages into a scratch consumer.

`accept` runs after a maintainer merged the pull request. It repeats the byte
check, stores the approved bytes as a Release of this repository
(`accepted/<name>@<version>`, never overwritten), writes the candidate
`candidates/<name>-<version>.json`, claims the name prefix in `prefixes.toml`,
and publishes into the real registry clone (deploy key) with a fast-forward
push.

Python 3.11+, standard library plus `jsonschema`. The pinned scripts in
`tools/vendor/` (`candidate.py`, `registry_candidate.py`) do the candidate
work; this file only orchestrates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "tools" / "vendor"
sys.path.insert(0, str(VENDOR))

import registry_candidate  # noqa: E402  (pinned copy, see tools/vendor/VENDORED.json)

SCHEMA_FILE = ROOT / "schema" / "registry-request.schema.json"
REQUEST_SCHEMA = "arxo.registry-request/1"
CANDIDATE_SCHEMA = "arxo.registry-candidate/1"
#: This repository; GITHUB_REPOSITORY in Actions (a staging copy rehearses with its own name).
INTAKE_REPOSITORY = os.environ.get("GITHUB_REPOSITORY") or "arxohq/registry-requests"
PUBLIC_REGISTRY = "https://github.com/arxohq/registry.git"
REGISTRY_ID = "arxo"
#: One release asset (GitHub allows 2 GiB; a package file is far smaller).
MAX_FILE_BYTES = 64 * 1024 * 1024
#: All files of one request together.
MAX_REQUEST_BYTES = 256 * 1024 * 1024
REQUEST_PATH = re.compile(r"^requests/([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)-([0-9A-Za-z][0-9A-Za-z._+-]*)/request\.json$")
API = "https://api.github.com"


class IntakeError(Exception):
    """A refusal: the message goes to the report, nothing is published."""


@dataclass
class Report:
    request: str = ""
    ok: bool = True
    lines: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    packages: list[dict] = field(default_factory=list)

    def refuse(self, message: str) -> None:
        self.ok = False
        self.refusals.append(message)

    def markdown(self) -> str:
        head = "✅ machine checks passed" if self.ok else "❌ request refused"
        out = [f"### Arxo registry intake — {head}", "", f"Request: `{self.request}`", ""]
        if self.packages:
            out += ["| package | version | contentHash | worldHash |", "|---|---|---|---|"]
            for p in self.packages:
                out.append(f"| `{p['name']}` | `{p['version']}` | `{p.get('contentHash', '')}` | `{p.get('worldHash', '')}` |")
            out.append("")
        if self.refusals:
            out += ["**Refusals**", ""] + [f"- {r}" for r in self.refusals] + [""]
        if self.lines:
            out += ["<details><summary>log</summary>", "", "```"] + self.lines + ["```", "</details>"]
        if self.ok:
            out += ["", "A maintainer still checks the license, the sources and the name "
                    "(see the checklist in README). Merging approves exactly the sha256 above."]
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------- validation

def load_schema() -> dict:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def validate_request(request: object, where: str) -> list[dict]:
    """Schema + candidate-manifest rules (paths, duplicates, limits); returns rows."""
    import jsonschema

    validator = jsonschema.Draft202012Validator(load_schema())
    errors = sorted(validator.iter_errors(request), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        at = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise IntakeError(f"{where}: schema {REQUEST_SCHEMA}: {at}: {first.message}")
    assert isinstance(request, dict)
    manifest = {"schema": CANDIDATE_SCHEMA, "packages": request["packages"]}
    try:
        return registry_candidate.validate_manifest(
            manifest, where=where, max_file_bytes=MAX_FILE_BYTES, max_total_bytes=MAX_REQUEST_BYTES)
    except registry_candidate.CandidateError as error:
        raise IntakeError(str(error)) from None


def artifacts_of(rows: list[dict]) -> list[tuple[str, str]]:
    return sorted({(r["name"], r["version"]) for r in rows})


def check_request_location(path: str, rows: list[dict]) -> tuple[str, str]:
    match = REQUEST_PATH.fullmatch(path)
    if not match:
        raise IntakeError(f"{path}: a request lives at requests/<name>-<version>/request.json")
    name, version = match.groups()
    if (name, version) not in artifacts_of(rows):
        raise IntakeError(f"{path}: the directory names {name}@{version}, which the request does not carry")
    return name, version


def prefix_of(name: str) -> str:
    return name.split(".", 1)[0]


def load_prefixes() -> tuple[set[str], dict[str, str]]:
    reserved = tomllib.loads((ROOT / "reserved-prefixes.toml").read_text(encoding="utf-8"))
    claimed = tomllib.loads((ROOT / "prefixes.toml").read_text(encoding="utf-8"))
    return set(reserved.get("reserved", [])), {k: v["owner"] for k, v in claimed.get("prefix", {}).items()}


def check_prefixes(rows: list[dict], repository: str) -> list[str]:
    """Every package prefix is free or already held by the same owner; returns new claims."""
    reserved, claimed = load_prefixes()
    owner = repository.split("/", 1)[0].lower()
    new: list[str] = []
    for name, _ in artifacts_of(rows):
        prefix = prefix_of(name)
        if prefix in reserved:
            raise IntakeError(f"{name}: prefix `{prefix}` is reserved for Arxo packages")
        holder = claimed.get(prefix)
        if holder is not None and holder.lower() != owner:
            raise IntakeError(f"{name}: prefix `{prefix}` belongs to {holder}; requests with it come only from that owner")
        if holder is None and prefix not in new:
            new.append(prefix)
    return new


# ---------------------------------------------------------------- downloads

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def http_get(url: str, headers: dict[str, str] | None = None, limit: int | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "arxo-registry-intake", **(headers or {})})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(limit + 1 if limit is not None else -1)
    if limit is not None and len(data) > limit:
        raise IntakeError(f"{url}: more than {limit} bytes")
    return data


def api_json(path: str) -> object:
    """The public GitHub API, unauthenticated by design (no Arxo secret in the check)."""
    try:
        return json.loads(http_get(API + path, {"Accept": "application/vnd.github+json"}))
    except urllib.error.HTTPError as error:
        raise IntakeError(f"GitHub API {path}: HTTP {error.code}") from None


def tag_commit(repository: str, tag: str) -> str:
    ref = api_json(f"/repos/{repository}/git/ref/tags/{urllib.parse.quote(tag, safe='')}")
    obj = ref["object"]
    for _ in range(4):
        if obj["type"] == "commit":
            return obj["sha"]
        if obj["type"] != "tag":
            break
        obj = api_json(f"/repos/{repository}/git/tags/{obj['sha']}")["object"]
    raise IntakeError(f"{repository}@{tag}: the tag does not point to a commit")


def download_release(request: dict, rows: list[dict], out: Path, report: Report) -> None:
    """Fetch every listed asset of the author's Release; size and sha256 must match."""
    source = request["source"]
    repository, tag = source["repository"], source["release"]
    if repository.lower().startswith("arxohq/"):
        raise IntakeError(f"{repository}: the bytes come from the author's own repository, not from arxohq")
    meta = api_json(f"/repos/{repository}")
    if meta.get("private"):
        raise IntakeError(f"{repository}: the repository is not public")
    commit = tag_commit(repository, tag)
    if commit != source["commit"]:
        raise IntakeError(f"{repository}@{tag}: tag points to {commit}, the request names {source['commit']}")
    release = api_json(f"/repos/{repository}/releases/tags/{urllib.parse.quote(tag, safe='')}")
    if release.get("draft"):
        raise IntakeError(f"{repository}@{tag}: a draft Release is not public")
    assets = {a["name"]: a for a in release.get("assets", [])}
    out.mkdir(parents=True, exist_ok=True)
    for row in rows:
        asset = assets.get(row["file"])
        if asset is None:
            raise IntakeError(f"{repository}@{tag}: no asset {row['file']}")
        if asset.get("size") != row["bytes"]:
            raise IntakeError(f"{row['file']}: asset has {asset.get('size')} bytes, the request says {row['bytes']}")
        data = http_get(asset["browser_download_url"], limit=MAX_FILE_BYTES)
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != row["bytes"] or digest != row["sha256"]:
            raise IntakeError(f"{row['file']}: sha256 {digest} ≠ {row['sha256']} named by the request")
        (out / row["file"]).write_bytes(data)
        report.lines.append(f"ok {row['file']} {digest}")


# ---------------------------------------------------------------- publishing

def run(command: list[str], report: Report, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    report.lines.append("$ " + " ".join(command))
    report.lines += [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()][-40:]
    if check and proc.returncode != 0:
        lines = (proc.stdout + "\n" + proc.stderr).strip().splitlines()
        coded = [line.strip() for line in lines if "REFUSAL" in line or "LPK-" in line or "LDC-" in line]
        reason = coded[0] if coded else (lines[-1].strip() if lines else str(proc.returncode))
        raise IntakeError(f"`{Path(command[0]).name} {' '.join(command[1:3])}` failed: {reason}")
    return proc


def restore(request: dict, bytes_dir: Path, artifacts: Path, work: Path) -> list[Path]:
    manifest = work / "candidate-for-restore.json"
    manifest.write_text(json.dumps({"schema": CANDIDATE_SCHEMA, "packages": request["packages"]}, indent=2) + "\n")
    try:
        return registry_candidate.restore(manifest, bytes_dir, artifacts,
                                          max_file_bytes=MAX_FILE_BYTES, max_total_bytes=MAX_REQUEST_BYTES)
    except registry_candidate.CandidateError as error:
        raise IntakeError(str(error)) from None


def release_summary(directories: list[Path], report: Report) -> None:
    for directory in directories:
        release = json.loads((directory / "release.json").read_text(encoding="utf-8"))
        for dep in release.get("dependencies") or []:
            if dep.get("registryId") != REGISTRY_ID:
                raise IntakeError(
                    f"{release['name']}@{release['version']}: dependency {dep.get('name')}@{dep.get('version')} "
                    f"comes from registry `{dep.get('registryId')}`; only `{REGISTRY_ID}` dependencies are accepted (LPK-E0603)")
        report.packages.append({k: release.get(k) for k in ("name", "version", "contentHash", "worldHash")})


def publish_into(artifacts: Path, registry: Path, law: Path, report: Report) -> None:
    command = [sys.executable, str(VENDOR / "registry_candidate.py"), "publish", str(artifacts),
               "--registry", str(registry), "--law", str(law), "--registry-id", REGISTRY_ID]
    run(command, report)


def registry_gate(registry: Path, base: str, report: Report) -> None:
    run([sys.executable, "check_registry.py", "--base", base], report, cwd=registry)


def consumer_install(rows: list[dict], registry: Path, law: Path, work: Path, report: Report) -> None:
    consumer = work / "consumer"
    run([str(law), "init", str(consumer), "--name", "intake.consumer"], report)
    for name, version in artifacts_of(rows):
        run([str(law), "add", f"{name}@{version}", "--project", str(consumer), "--from", REGISTRY_ID,
             "--registry", f"{REGISTRY_ID}={registry}"], report)
    run([str(law), "install", "--project", str(consumer), "--offline"], report)


def changed_requests(base: str, head: str = "HEAD", repo: Path = ROOT) -> tuple[list[str], list[str]]:
    """(request files added, every other changed path) between base and head."""
    out = subprocess.run(["git", "diff", "--name-status", "--no-renames", f"{base}...{head}"],
                         cwd=repo, check=True, capture_output=True, text=True).stdout
    added, other = [], []
    for line in out.splitlines():
        status, path = line.split("\t", 1)
        if status == "A" and REQUEST_PATH.fullmatch(path):
            added.append(path)
        else:
            other.append(f"{status} {path}")
    return added, other


def read_request(path: str, repo: Path = ROOT) -> tuple[dict, list[dict]]:
    try:
        request = json.loads((repo / path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise IntakeError(f"{path}: {error}") from None
    rows = validate_request(request, path)
    check_request_location(path, rows)
    return request, rows


def verify_request(path: str, law: Path, work: Path, registry_url: str, report: Report,
                   repo: Path = ROOT) -> tuple[dict, list[dict], list[str]]:
    """Everything up to and including a publication into a scratch registry clone."""
    request, rows = read_request(path, repo)
    new_prefixes = check_prefixes(rows, request["source"]["repository"])
    bytes_dir = work / "bytes"
    download_release(request, rows, bytes_dir, report)
    directories = restore(request, bytes_dir, work / "artifacts", work)
    release_summary(directories, report)
    registry = work / "registry"
    run(["git", "clone", "--quiet", registry_url, str(registry)], report)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=registry, check=True,
                          capture_output=True, text=True).stdout.strip()
    publish_into(work / "artifacts", registry, law, report)
    registry_gate(registry, base, report)
    consumer_install(rows, registry, law, work, report)
    return request, rows, new_prefixes


def cmd_check(args: argparse.Namespace) -> int:
    report = Report()
    try:
        # The tools, schema and prefix lists come from the trusted base checkout
        # (this file's ROOT); only the request comes from the pull request.
        added, other = changed_requests(args.base, "HEAD", args.pr_root)
        if other:
            raise IntakeError("a request pull request adds one requests/<name>-<version>/request.json and "
                              "changes nothing else; also changed: " + ", ".join(other[:10]))
        if len(added) != 1:
            raise IntakeError(f"one request per pull request, found {len(added)}")
        report.request = added[0]
        if args.work.exists():
            shutil.rmtree(args.work)
        args.work.mkdir(parents=True)
        verify_request(added[0], args.law, args.work, args.registry_url, report, args.pr_root)
    except IntakeError as error:
        report.refuse(str(error))
    except Exception as error:  # an unexpected failure is a refusal too, never a pass
        report.refuse(f"internal error: {type(error).__name__}: {error}")
    text = report.markdown()
    print(text)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    return 0 if report.ok else 1


# ---------------------------------------------------------------- acceptance

def gh(args: list[str], report: Report, check: bool = True) -> subprocess.CompletedProcess:
    return run(["gh", *args], report, check=check)


def store_release(name: str, version: str, rows: list[dict], bytes_dir: Path, report: Report) -> str:
    """Release `accepted/<name>@<version>` with exactly the approved bytes; never overwritten."""
    tag = f"accepted/{name}@{version}"
    view = gh(["release", "view", tag, "--repo", INTAKE_REPOSITORY, "--json", "assets"], report, check=False)
    if view.returncode == 0:
        present = {a["name"]: a for a in json.loads(view.stdout)["assets"]}
        for row in rows:
            asset = present.get(row["file"])
            digest = (asset or {}).get("digest", "")
            if asset is None or asset.get("size") != row["bytes"] or (digest and digest != f"sha256:{row['sha256']}"):
                raise IntakeError(f"{tag} exists with other bytes for {row['file']}; accepted bytes are never replaced")
        report.lines.append(f"{tag} already stored")
        return tag
    gh(["release", "create", tag, "--repo", INTAKE_REPOSITORY, "--prerelease", "--title", tag,
        "--target", os.environ.get("GITHUB_SHA", "main"),
        "--notes", f"Accepted bytes of {name}@{version}. Never edited; the registry is published from these assets."],
       report)
    gh(["release", "upload", tag, "--repo", INTAKE_REPOSITORY, *[str(bytes_dir / r["file"]) for r in rows]], report)
    return tag


def write_candidate(name: str, version: str, rows: list[dict], tag: str) -> Path:
    path = ROOT / "candidates" / f"{name}-{version}.json"
    candidate = {"schema": CANDIDATE_SCHEMA, "published": False,
                 "assets": {"release": tag, "repository": INTAKE_REPOSITORY},
                 "packages": rows}
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def claim_prefixes(prefixes: list[str], owner: str, request_path: str) -> None:
    path = ROOT / "prefixes.toml"
    text = path.read_text(encoding="utf-8")
    held = tomllib.loads(text).get("prefix", {})
    for prefix in prefixes:
        if prefix in held:
            continue
        text += f'\n[prefix.{prefix}]\nowner = "{owner}"\nfirst = "{request_path}"\n'
    path.write_text(text, encoding="utf-8")


def git(args: list[str], report: Report, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    return run(["git", *args], report, cwd=cwd, check=check)


def push_with_retry(cwd: Path, branch: str, report: Report, redo=None, attempts: int = 4) -> None:
    """Fast-forward push; on a race fetch, rebuild on the new tip (redo) and retry."""
    for attempt in range(attempts):
        if git(["push", "origin", f"HEAD:{branch}"], report, cwd=cwd, check=False).returncode == 0:
            return
        report.lines.append(f"push rejected (attempt {attempt + 1}); rebuilding on the new tip")
        git(["fetch", "origin", branch], report, cwd=cwd)
        if redo is None:
            git(["rebase", f"origin/{branch}"], report, cwd=cwd)
        else:
            redo()
    raise IntakeError(f"push to {branch} kept racing after {attempts} attempts")


def cmd_accept(args: argparse.Namespace) -> int:
    report = Report()
    try:
        added, _ = changed_requests(args.before, "HEAD")
        if not added:
            print("no new requests")
            return 0
        for path in added:
            report.request = path
            name, version = REQUEST_PATH.fullmatch(path).groups()
            existing = ROOT / "candidates" / f"{name}-{version}.json"
            if existing.exists() and json.loads(existing.read_text(encoding="utf-8")).get("published"):
                report.lines.append(f"{path}: already accepted and published")
                continue
            # A candidate without `published` is an interrupted acceptance: resume it.
            # Every step below is idempotent (the stored Release is compared, not replaced).
            work = args.work / f"{name}-{version}"
            if work.exists():
                shutil.rmtree(work)
            work.mkdir(parents=True)
            # 1. The same machine checks against the public registry, bytes re-downloaded.
            request, rows, new_prefixes = verify_request(path, args.law, work / "verify", PUBLIC_REGISTRY, report)
            # 2. Bytes under Arxo's control.
            tag = store_release(name, version, rows, work / "verify" / "bytes", report)
            # 3. Candidate + prefix claim in this repository.
            write_candidate(name, version, rows, tag)
            owner = request["source"]["repository"].split("/", 1)[0]
            claim_prefixes(new_prefixes, owner, path)
            git(["add", "candidates", "prefixes.toml"], report)
            if git(["diff", "--cached", "--quiet"], report, check=False).returncode != 0:
                git(["-c", "user.name=arxo registry intake", "-c", "user.email=hi@arxo.io",
                     "commit", "-m", f"accept {name}@{version} from {request['source']['repository']}"], report)
                push_with_retry(ROOT, "main", report)
            # 4. Publish from the accepted Release (not from the author's) into the registry.
            candidate_path = ROOT / "candidates" / f"{name}-{version}.json"
            fetched = work / "accepted-bytes"
            run([sys.executable, str(VENDOR / "candidate.py"), "fetch", str(candidate_path), "--out", str(fetched)], report)
            artifacts = work / "accepted-artifacts"
            run([sys.executable, str(VENDOR / "registry_candidate.py"), "restore", str(candidate_path), str(fetched),
                 "--out", str(artifacts)], report)
            registry = work / "registry"
            git(["clone", "--quiet", args.registry_remote, str(registry)], report)

            def build() -> None:
                git(["reset", "--quiet", "--hard", "origin/main"], report, cwd=registry)
                git(["clean", "-fdq"], report, cwd=registry)
                base = git(["rev-parse", "HEAD"], report, cwd=registry).stdout.strip()
                publish_into(artifacts, registry, args.law, report)
                registry_gate(registry, base, report)
                if git(["status", "--porcelain"], report, cwd=registry).stdout.strip():
                    git(["add", "-A"], report, cwd=registry)
                    git(["-c", "user.name=arxo registry intake", "-c", "user.email=hi@arxo.io", "commit", "-m",
                         f"publish {name}@{version} accepted in {INTAKE_REPOSITORY}"], report, cwd=registry)

            git(["fetch", "--quiet", "origin", "main"], report, cwd=registry)
            build()
            push_with_retry(registry, "main", report, redo=build)
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            candidate["published"] = True
            candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            git(["add", str(candidate_path)], report)
            git(["-c", "user.name=arxo registry intake", "-c", "user.email=hi@arxo.io",
                 "commit", "-m", f"{name}@{version} published to arxo"], report)
            push_with_retry(ROOT, "main", report)
    except IntakeError as error:
        report.refuse(str(error))
    except Exception as error:
        report.refuse(f"internal error: {type(error).__name__}: {error}")
    text = report.markdown()
    print(text)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("check", help="machine checks of a pull request, no secrets, nothing published")
    c.add_argument("--base", required=True, help="base ref of the pull request")
    c.add_argument("--law", type=Path, required=True)
    c.add_argument("--work", type=Path, required=True)
    c.add_argument("--report", type=Path)
    c.add_argument("--registry-url", default=PUBLIC_REGISTRY, help="registry to clone into scratch")
    c.add_argument("--pr-root", type=Path, default=ROOT, help="checkout of the pull request head")
    a = sub.add_parser("accept", help="store, record and publish merged requests")
    a.add_argument("--before", required=True, help="the push's previous main")
    a.add_argument("--law", type=Path, required=True)
    a.add_argument("--work", type=Path, required=True)
    a.add_argument("--report", type=Path)
    a.add_argument("--registry-remote", required=True, help="registry clone URL with write access (deploy key)")
    args = parser.parse_args(argv)
    args.law = args.law.resolve()
    args.work = args.work.resolve()
    if args.mode == "check":
        args.pr_root = args.pr_root.resolve()
    return cmd_check(args) if args.mode == "check" else cmd_accept(args)


if __name__ == "__main__":
    sys.exit(main())
