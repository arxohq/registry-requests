#!/usr/bin/env python3
"""Кандидат реестра arxo (DECISION-0384 DX11, механика DECISION-0376, DECISION-0476).

В git — только манифест `apps/delivery/candidates/registry.json`, который
пишет `law publish <артефакт> --candidate … --bytes … --release <тег>`:
строки {name, version, file, path, bytes, sha256} на каждый файл артефакта
`law pack`, плоско (`<name>-<version>.<путь через точки>`). Байты уезжают
ассетами GitHub Release (`candidate.py upload`); джоба `candidate.py fetch`
скачивает их, сверяет sha256, а этот скрипт восстанавливает каталоги артефактов
и издаёт каждый закреплённым `law publish --registry arxo=<checkout>`.

    python3 apps/delivery/registry_candidate.py check <манифест>
    python3 apps/delivery/registry_candidate.py restore <манифест> <байты> --out <каталог>
    python3 apps/delivery/registry_candidate.py publish <каталог артефактов> --registry <checkout> --law <бинарь> [--key <файл>]

Манифест считается недоверенным (DECISION-0476 п. 7б): после принятия внешней
заявки его пишет CI, но проверка не полагается на это. До записи чего-либо
проверяются схема, типы и значения каждой строки, уникальность
`name+version+path` и `file`, отсутствие разделителей в `file`, относительный
`path` без `..` и абсолютных частей, пределы размеров; `restore` сверяет
sha256 и длину всех байтов и только затем пишет каталоги. Порядок издания —
топологический по `dependencies` из `release.json` каждого артефакта:
зависимость того же `registryId` издаётся раньше потребителя, цикл — отказ.

Токенов у авторов нет по построению: издаёт только джоба после слияния.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

SCHEMA = "arxo.registry-candidate/1"
RELEASE_FILE = "release.json"
REQUIRED_TEXT = ("name", "version", "file", "path", "sha256")
# Пределы по умолчанию: один файл артефакта (CLIR, мир, лок, ресурс) и весь
# манифест. Джоба может поднять их флагами; нулевые и отрицательные — отказ.
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ROWS = 4096
MAX_TEXT = 512
# Те же правила, что у резолвера (law-resolver::manifest): имя пакета и версия.
PACKAGE_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class CandidateError(SystemExit):
    """Отказ оснастки: сообщение печатается, код выхода 1, ничего не записано."""


def _text(row: dict, key: str, where: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise CandidateError(f"{where}: поле {key} отсутствует или не непустая строка")
    if len(value) > MAX_TEXT:
        raise CandidateError(f"{where}: поле {key} длиннее {MAX_TEXT} символов")
    if "\x00" in value:
        raise CandidateError(f"{where}: поле {key} содержит NUL")
    return value


def check_file_name(name: str, where: str) -> str:
    """Имя ассета: одно имя файла без каталогов."""
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise CandidateError(f"{where}: file {name!r} содержит разделитель пути или не является именем файла")
    if name != name.strip() or any(c.isspace() for c in name):
        raise CandidateError(f"{where}: file {name!r} содержит пробельные символы")
    return name


def check_relative_path(path: str, where: str) -> str:
    """Путь внутри артефакта: относительный POSIX-путь без `..`, `.` и пустых частей."""
    pure = PurePosixPath(path)
    if ("\\" in path or pure.is_absolute() or path.endswith("/")
            or pure.as_posix() != path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or ":" in pure.parts[0]):
        raise CandidateError(f"{where}: path {path!r} не относительный путь внутри артефакта")
    return path


def _assets(manifest: dict, where: str) -> dict:
    assets = manifest.get("assets", {})
    if assets is None:
        return {}
    if not isinstance(assets, dict):
        raise CandidateError(f"{where}: assets не объект")
    for key in ("release", "repository"):
        if key in assets:
            value = assets[key]
            if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
                raise CandidateError(f"{where}: assets.{key} не непустая строка")
    if "repository" in assets and not REPOSITORY.fullmatch(assets["repository"]):
        raise CandidateError(f"{where}: assets.repository {assets['repository']!r} не owner/repo")
    if "release" in assets and ("/" in assets["release"] or any(c.isspace() for c in assets["release"])):
        raise CandidateError(f"{where}: assets.release {assets['release']!r} не тег")
    return assets


def validate_manifest(manifest: object, *, where: str = "манифест",
                      max_file_bytes: int = MAX_FILE_BYTES,
                      max_total_bytes: int = MAX_TOTAL_BYTES) -> list[dict]:
    """Проверить манифест кандидата целиком; вернуть нормализованные строки.

    Нормализация: `sha256` без префикса `sha256:`. Никаких записей на диск.
    """
    if max_file_bytes <= 0 or max_total_bytes <= 0:
        raise CandidateError(f"{where}: пределы размеров должны быть положительными")
    if not isinstance(manifest, dict):
        raise CandidateError(f"{where}: не объект JSON")
    if manifest.get("schema") != SCHEMA:
        raise CandidateError(f"{where}: не манифест {SCHEMA}")
    _assets(manifest, where)
    packages = manifest.get("packages")
    if not isinstance(packages, list) or not packages:
        raise CandidateError(f"{where}: packages отсутствует или пуст")
    if len(packages) > MAX_ROWS:
        raise CandidateError(f"{where}: строк больше {MAX_ROWS}")
    rows: list[dict] = []
    seen_keys: set[tuple[str, str, str]] = set()
    seen_files: set[str] = set()
    artifacts: dict[tuple[str, str], set[str]] = {}
    total = 0
    for index, row in enumerate(packages):
        at = f"{where}: packages[{index}]"
        if not isinstance(row, dict):
            raise CandidateError(f"{at}: не объект")
        fields = {key: _text(row, key, at) for key in REQUIRED_TEXT}
        if not PACKAGE_NAME.fullmatch(fields["name"]):
            raise CandidateError(f"{at}: name {fields['name']!r} не каноническое имя пакета")
        if not VERSION.fullmatch(fields["version"]):
            raise CandidateError(f"{at}: version {fields['version']!r} не версия")
        check_file_name(fields["file"], at)
        check_relative_path(fields["path"], at)
        digest = fields["sha256"].removeprefix("sha256:")
        if not SHA256.fullmatch(digest):
            raise CandidateError(f"{at}: sha256 {fields['sha256']!r} не 64 hex-символа")
        size = row.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise CandidateError(f"{at}: bytes отсутствует или не целое ≥ 0")
        if size > max_file_bytes:
            raise CandidateError(f"{at}: bytes {size} больше предела {max_file_bytes}")
        total += size
        if total > max_total_bytes:
            raise CandidateError(f"{where}: суммарный размер больше предела {max_total_bytes}")
        key = (fields["name"], fields["version"], fields["path"])
        if key in seen_keys:
            raise CandidateError(f"{at}: дубликат name+version+path {key}")
        seen_keys.add(key)
        if fields["file"] in seen_files:
            raise CandidateError(f"{at}: дубликат file {fields['file']!r}")
        seen_files.add(fields["file"])
        artifacts.setdefault((fields["name"], fields["version"]), set()).add(fields["path"])
        rows.append({**fields, "sha256": digest, "bytes": size})
    for (name, version), paths in sorted(artifacts.items()):
        if RELEASE_FILE not in paths:
            raise CandidateError(f"{where}: {name}-{version}: в кандидате нет {RELEASE_FILE} — артефакт неполон")
    return rows


def load_manifest(manifest_path: Path, **limits) -> list[dict]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CandidateError(f"{manifest_path}: {error}") from error
    return validate_manifest(manifest, where=str(manifest_path), **limits)


def restore(manifest_path: Path, bytes_dir: Path, out: Path, **limits) -> list[Path]:
    """Восстановить каталоги артефактов из плоских байтов.

    Порядок строгий: полная проверка манифеста → сверка sha256 и длины всех
    байтов → запись. До последнего шага каталог `out` не трогается.
    """
    rows = load_manifest(manifest_path, **limits)
    verified: list[tuple[Path, bytes]] = []
    out_root = out.resolve()
    for row in rows:
        source = bytes_dir / row["file"]
        if source.is_symlink() or not source.is_file():
            raise CandidateError(f"{source}: ассета нет или это не обычный файл")
        data = source.read_bytes()
        if len(data) != row["bytes"]:
            raise CandidateError(f"{source}: {len(data)} байт ≠ {row['bytes']} из манифеста")
        actual = hashlib.sha256(data).hexdigest()
        if actual != row["sha256"]:
            raise CandidateError(f"{source}: sha256 {actual} не совпадает с манифестом {row['sha256']}")
        target = out_root / f"{row['name']}-{row['version']}" / row["path"]
        # Защита в глубину: путь уже проверен, но итоговый адрес обязан лежать под out.
        if out_root not in target.resolve().parents:
            raise CandidateError(f"{manifest_path}: {row['path']!r} выходит за {out}")
        verified.append((target, data))
    if out.exists() and any(out.iterdir()):
        raise CandidateError(f"{out}: каталог не пуст — восстановление пишет только в новый или пустой каталог")
    for target, data in verified:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    directories = sorted({out_root / f"{row['name']}-{row['version']}" for row in rows})
    for directory in directories:
        release = read_release(directory)
        expected = f"{release['name']}-{release['version']}"
        if directory.name != expected:
            raise CandidateError(f"{directory}: release.json называет {expected}, каталог — {directory.name}")
    return directories


def read_release(directory: Path) -> dict:
    path = directory / RELEASE_FILE
    try:
        release = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CandidateError(f"{path}: {error}") from error
    if not isinstance(release, dict):
        raise CandidateError(f"{path}: не объект")
    for key in ("name", "version"):
        _text(release, key, str(path))
    deps = release.get("dependencies", [])
    if deps is None:
        deps = []
    if not isinstance(deps, list) or any(not isinstance(d, dict) for d in deps):
        raise CandidateError(f"{path}: dependencies не список объектов")
    release["dependencies"] = deps
    return release


def publication_order(releases: dict[Path, dict], registry_id: str) -> list[Path]:
    """Топологический порядок: зависимость того же реестра раньше потребителя.

    Ребро строится только к пакету из этого же набора кандидатов с тем же
    именем и версией; зависимости на уже изданные версии рёбер не дают.
    Среди готовых к изданию порядок по имени и версии — детерминирован.
    Цикл — отказ с перечислением участников.
    """
    by_key: dict[tuple[str, str], Path] = {}
    for path, release in releases.items():
        key = (release["name"], release["version"])
        if key in by_key:
            raise CandidateError(f"{path} и {by_key[key]}: один и тот же пакет {key[0]}@{key[1]} дважды")
        by_key[key] = path
    edges: dict[Path, set[Path]] = {path: set() for path in releases}
    for path, release in releases.items():
        for dep in release["dependencies"]:
            if dep.get("registryId") != registry_id:
                continue
            target = by_key.get((dep.get("name"), dep.get("version")))
            if target is None:
                continue
            if target == path:
                raise CandidateError(f"{path}: пакет зависит сам от себя")
            edges[path].add(target)
    label = lambda p: (releases[p]["name"], releases[p]["version"])  # noqa: E731
    remaining = dict(edges)
    order: list[Path] = []
    while remaining:
        ready = sorted((p for p, deps in remaining.items() if not deps), key=label)
        if not ready:
            cycle = ", ".join(f"{n}@{v}" for n, v in sorted(label(p) for p in remaining))
            raise CandidateError(f"цикл зависимостей среди кандидатов: {cycle}")
        for path in ready:
            order.append(path)
            del remaining[path]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


def publish(artifacts_dir: Path, registry: Path, law: Path, registry_id: str,
            key: Path | None = None, runner=subprocess.run) -> int:
    dirs = sorted(p for p in artifacts_dir.iterdir() if (p / RELEASE_FILE).is_file())
    if not dirs:
        raise CandidateError(f"{artifacts_dir}: артефактов нет")
    if key is not None and not key.is_file():
        raise CandidateError(f"{key}: файла ключа нет")
    releases = {path: read_release(path) for path in dirs}
    for path in publication_order(releases, registry_id):
        command = [str(law.resolve()), "publish", str(path), "--registry", f"{registry_id}={registry}"]
        if key is not None:
            command += ["--key", str(key)]
        proc = runner(command, check=False)
        if proc.returncode != 0:
            return proc.returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    limits = argparse.ArgumentParser(add_help=False)
    limits.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES)
    limits.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL_BYTES)
    c = sub.add_parser("check", parents=[limits], help="проверить манифест, ничего не записывая")
    c.add_argument("manifest", type=Path)
    r = sub.add_parser("restore", parents=[limits])
    r.add_argument("manifest", type=Path)
    r.add_argument("bytes_dir", type=Path)
    r.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("publish")
    p.add_argument("artifacts", type=Path)
    p.add_argument("--registry", type=Path, required=True)
    p.add_argument("--law", type=Path, required=True)
    p.add_argument("--registry-id", default="arxo")
    p.add_argument("--key", type=Path, help="ключ издателя формата `law key new`, прокидывается в law publish")
    args = parser.parse_args(argv)
    if args.mode == "check":
        rows = load_manifest(args.manifest, max_file_bytes=args.max_file_bytes,
                             max_total_bytes=args.max_total_bytes)
        artifacts = sorted({(r["name"], r["version"]) for r in rows})
        print(f"ok {args.manifest}: {len(rows)} файлов, {len(artifacts)} артефактов")
        return 0
    if args.mode == "restore":
        for path in restore(args.manifest, args.bytes_dir, args.out,
                            max_file_bytes=args.max_file_bytes, max_total_bytes=args.max_total_bytes):
            print(path)
        return 0
    return publish(args.artifacts, args.registry, args.law, args.registry_id, args.key)


if __name__ == "__main__":
    sys.exit(main())
