#!/usr/bin/env python3
"""Байты кандидата выпуска по закоммиченному манифесту (DECISION-0376).

В git лежит только манифест кандидата — `apps/delivery/candidates/<канал>.json`:
имя, файл и sha256 каждого пакета. Сами tarball-ы и колёса в git не лежат:

- до выпуска — ассеты GitHub Release, тег которого манифест называет полем
  `assets.release` (его ставит `upload`); репозиторий Release — необязательное
  поле `assets.repository` (`owner/repo`, DECISION-0476: принятые байты внешней
  заявки лежат в репозитории приёма), без него — репозиторий самой джобы
  (`GITHUB_REPOSITORY`, иначе `origin` этого checkout-а);
- после выпуска — реестр: npm отдаёт ровно загруженный tarball, PyPI — ровно
  загруженное колесо, поэтому sha256 манифеста сверяется и с ними.

Публикует по-прежнему только джоба (DECISION-0064), и публикует ровно байты,
чей sha256 назван в манифесте коммита: `fetch` отказывает на любом расхождении.

    python3 apps/delivery/candidate.py fetch <манифест> --out <каталог> [--manifest-name <имя>]
    python3 apps/delivery/candidate.py upload <манифест> <каталог байтов> --release <тег>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NPM = "https://registry.npmjs.org"
PYPI = "https://pypi.org/pypi"


class CandidateError(SystemExit):
    pass


def relative(row: dict) -> str:
    """Путь файла пакета внутри каталога кандидата."""
    if "tarball" in row:  # arxo.mcp-npm-release/1: release.py читает tarballs/
        return f"tarballs/{row['tarball']}"
    return row["file"]


def expected_sha(row: dict) -> str:
    return row["sha256"].removeprefix("sha256:")


def version_of(row: dict) -> str:
    if row.get("version"):
        return row["version"]
    name = Path(row["file"]).name
    if name.endswith(".whl"):
        return name.split("-")[1]
    return name.removesuffix(".tgz").rsplit("-", 1)[1]


def http_get(url: str, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def from_npm(row: dict, registry: str) -> bytes:
    name = urllib.parse.quote(row["name"], safe="@")
    meta = json.loads(http_get(f"{registry}/{name}/{urllib.parse.quote(version_of(row))}",
                               {"Cache-Control": "no-cache"}))
    return http_get(meta["dist"]["tarball"])


def from_pypi(row: dict) -> bytes:
    meta = json.loads(http_get(f"{PYPI}/{row['name']}/{version_of(row)}/json"))
    for entry in meta.get("urls", []):
        if entry["filename"] == Path(row["file"]).name:
            return http_get(entry["url"])
    raise CandidateError(f"{row['name']}: на PyPI нет файла {row['file']}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - протокол urllib
        return None


REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def assets_repository(manifest: dict) -> str | None:
    """`assets.repository` манифеста (`owner/repo`) или None — репозиторий джобы."""
    repo = (manifest.get("assets") or {}).get("repository")
    if repo is None:
        return None
    if not isinstance(repo, str) or not REPOSITORY.fullmatch(repo):
        raise CandidateError(f"assets.repository {repo!r}: ожидается owner/repo")
    return repo


def github_repo() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        return repo
    url = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"],
                         check=True, capture_output=True, text=True).stdout.strip()
    # git@github.com-<псевдоним>:owner/repo.git и https://github.com/owner/repo(.git)
    match = re.search(r"([^/:]+/[^/:]+?)(?:\.git)?/?$", url)
    if match is None:
        raise CandidateError(f"origin {url}: не видно owner/repo; задайте GITHUB_REPOSITORY")
    return match.group(1)


def github_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    return subprocess.run(["gh", "auth", "token"], check=True,
                          capture_output=True, text=True).stdout.strip()


def from_release(tag: str, filename: str, cache: dict, repository: str | None = None) -> bytes:
    token = github_token()
    auth = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    if tag not in cache:
        repo = repository or github_repo()
        api = f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}"
        cache[tag] = {a["name"]: a["url"] for a in json.loads(http_get(api, auth))["assets"]}
    url = cache[tag].get(filename)
    if url is None:
        raise CandidateError(f"в релизе {tag} нет ассета {filename}")
    # Ассет отдаётся редиректом на подписанный адрес хранилища; заголовок
    # Authorization туда нести нельзя — хранилище отвергает второй способ входа.
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={**auth, "Accept": "application/octet-stream"})
    try:
        with opener.open(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as redirect:
        if redirect.code not in (301, 302, 303, 307, 308):
            raise
        return http_get(redirect.headers["Location"])


def fetch(manifest_path: Path, out: Path, manifest_name: str | None) -> int:
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    release = (manifest.get("assets") or {}).get("release")
    repository = assets_repository(manifest)
    registry = manifest.get("registry", NPM).rstrip("/")
    out.mkdir(parents=True, exist_ok=True)
    (out / (manifest_name or manifest_path.name)).write_bytes(raw)
    cache: dict = {}
    for row in manifest["packages"]:
        rel = relative(row)
        filename = Path(rel).name
        if release:
            where = f"релиз {repository}@{release}" if repository else f"релиз {release}"
            source, data = where, from_release(release, filename, cache, repository)
        elif filename.endswith(".whl"):
            source, data = "PyPI", from_pypi(row)
        else:
            source, data = registry, from_npm(row, registry)
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha(row):
            raise CandidateError(f"{filename} ({source}): sha256 {digest} ≠ {expected_sha(row)} "
                                 f"из {manifest_path}")
        if "bytes" in row and len(data) != row["bytes"]:
            raise CandidateError(f"{filename} ({source}): {len(data)} байт ≠ {row['bytes']}")
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print(f"ok {row['name']:30s} {filename} ← {source}")
    return 0


def upload(manifest_path: Path, directory: Path, tag: str) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = []
    for row in manifest["packages"]:
        path = directory / Path(relative(row)).name
        if not path.is_file():
            path = directory / relative(row)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_sha(row):
            raise CandidateError(f"{path}: sha256 {digest} ≠ манифесту {manifest_path}")
        files.append(str(path))
    exists = subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode == 0
    if not exists:
        # Не draft: черновик не виден токену джобы с contents: read.
        subprocess.run(["gh", "release", "create", tag, "--prerelease", "--title", tag,
                        "--target", subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                                                   capture_output=True, text=True).stdout.strip(),
                        "--notes", f"Байты кандидата {manifest_path.name} (DECISION-0376). "
                                   "Выпуск — только джобой канала."], check=True)
    # Без --clobber: подменить байты под тем же тегом нельзя.
    subprocess.run(["gh", "release", "upload", tag, *files], check=True)
    # `assets.repository` (если есть) сохраняется: upload меняет только тег.
    manifest["assets"] = {**(manifest.get("assets") or {}), "release": tag}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    print(f"{manifest_path}: assets.release = {tag}; закоммитьте манифест")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("fetch", help="скачать байты и сверить с манифестом")
    get.add_argument("manifest", type=Path)
    get.add_argument("--out", type=Path, required=True)
    get.add_argument("--manifest-name", help="имя копии манифеста в --out (для потребителя)")
    put = sub.add_parser("upload", help="выложить байты в GitHub Release и назвать тег в манифесте")
    put.add_argument("manifest", type=Path)
    put.add_argument("directory", type=Path)
    put.add_argument("--release", required=True)
    args = parser.parse_args()
    if args.command == "fetch":
        return fetch(args.manifest, args.out, args.manifest_name)
    return upload(args.manifest, args.directory, args.release)


if __name__ == "__main__":
    sys.exit(main())
