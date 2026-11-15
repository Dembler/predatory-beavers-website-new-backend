"""Create and verify a private deployment bundle; never restore or upload data."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from predatory_beavers.worker.sports_sync import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
IMAGE_SUFFIXES = {".webp", ".png", ".jpg", ".jpeg", ".gif", ".avif"}
WORKING_MANIFESTS = {
    "asb-roster.json",
    "asb-standings.json",
    "asb-team-logos.json",
    "results-asb.json",
    "results-fiba3x3.json",
}
SYNC_DOCUMENTS = {"status.json", "sources.json", "fiba-watchlist.json"}


def _no_links(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink() or component.is_junction():
            raise ValueError("Symbolic links and junctions are not supported")


def _relative_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Invalid bundle path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError("Bundle paths must stay inside the bundle")
    if path.as_posix() != value:
        raise ValueError("Bundle paths must be normalized")
    return path


def _allowed_file(path: PurePosixPath) -> bool:
    if path == PurePosixPath("data/local.db"):
        return True
    if any(
        part.startswith(".")
        or part.lower() in {"tmp", "temp", "backups", "locks"}
        or set(part.lower().split(".")) & {"tmp", "bak", "lock"}
        for part in path.parts
    ):
        return False
    if path.parts[:2] == ("data", "media") and len(path.parts) > 2:
        return path.suffix.lower() in IMAGE_SUFFIXES
    if path.parts[:2] != ("data", "sports-sync"):
        return False
    relative = path.parts[2:]
    if len(relative) == 1:
        return relative[0] in SYNC_DOCUMENTS
    if len(relative) == 2 and relative[0] == "roster-photos":
        return relative[1] == "index.json" or path.suffix.lower() == ".webp"
    return (
        len(relative) == 3
        and relative[:2] == ("working", "docs")
        and relative[2] in WORKING_MANIFESTS
    )


def _files(directory: Path) -> list[Path]:
    """Check links before entering directories, including Windows junctions."""
    _no_links(directory)
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValueError("Expected a directory")
    result = []
    pending = [directory]
    while pending:
        for path in sorted(pending.pop().iterdir()):
            _no_links(path)
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                result.append(path)
            else:
                raise ValueError("Only regular files and directories are supported")
    return sorted(result)


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _integrity_check(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")


def _database_copy(database: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(destination)) as target,
    ):
        source.backup(target)
        _integrity_check(target)
        if target.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone():
            target.execute("DELETE FROM sessions")
            target.commit()
        # Remove deleted token hashes from unused pages, too. User password hashes remain.
        target.execute("VACUUM")
        target.execute("PRAGMA journal_mode=DELETE")
        _integrity_check(target)


def export_bundle(database: Path, media: Path, output: Path | None = None) -> Path:
    database, media = database.absolute(), media.absolute()
    _no_links(database)
    _no_links(media)
    if not database.is_file():
        raise ValueError("The source database does not exist")
    if not media.is_dir():
        raise ValueError("The source media directory does not exist")
    destination = (
        output.absolute()
        if output is not None
        else ROOT / "data/deployment" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    )
    _no_links(destination)
    sync = database.parent / "sports-sync"
    if destination.is_relative_to(media) or destination.is_relative_to(sync):
        raise ValueError("The bundle must be outside source media and sports-sync")
    if destination.exists():
        raise FileExistsError("The output already exists; choose a new directory")
    lock = ProcessLock(database.with_name(database.name + ".sports-sync.lock"))
    if not lock.acquire():
        raise RuntimeError("Sports synchronization is running; retry when it finishes")
    try:
        destination.mkdir(parents=True, exist_ok=False)
        _database_copy(database, destination / "data/local.db")
        for source_root, relative_root in (
            (media, PurePosixPath("data/media")),
            (sync, PurePosixPath("data/sports-sync")),
        ):
            for source in _files(source_root):
                relative = relative_root / source.relative_to(source_root).as_posix()
                if _allowed_file(relative):
                    target = destination / relative.as_posix()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
        files = [
            {
                "path": file.relative_to(destination).as_posix(),
                "size": file.stat().st_size,
                "sha256": _hash(file),
            }
            for file in _files(destination)
        ]
        manifest = {
            "format_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "sessions_removed": True,
            "files": files,
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        verify_bundle(destination)
        return destination
    finally:
        lock.release()


def verify_bundle(bundle: Path) -> dict[str, Any]:
    bundle = bundle.absolute()
    actual = {path.relative_to(bundle).as_posix() for path in _files(bundle)}
    if "manifest.json" not in actual:
        raise ValueError("Bundle manifest is missing")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("format_version") != 1
        or manifest.get("sessions_removed") is not True
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("Unsupported bundle manifest")
    expected = {"manifest.json"}
    for record in manifest["files"]:
        if not isinstance(record, dict):
            raise ValueError("Invalid file record")
        relative = _relative_path(record.get("path"))
        name = relative.as_posix()
        size, digest = record.get("size"), record.get("sha256")
        if (
            name in expected
            or not _allowed_file(relative)
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("Invalid or duplicate file record")
        expected.add(name)
        file = bundle / name
        if name not in actual or file.stat().st_size != size or _hash(file) != digest:
            raise ValueError("A bundle file is missing or has changed")
    if expected != actual:
        raise ValueError("Unexpected files in the bundle")
    if "data/local.db" not in expected:
        raise ValueError("Bundle database is missing")
    with closing(
        sqlite3.connect((bundle / "data/local.db").as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        _integrity_check(connection)
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone() and connection.execute("SELECT COUNT(*) FROM sessions").fetchone() != (0,):
            raise ValueError("Bundle contains active or revoked login sessions")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Create a NEW private bundle directory")
    export.add_argument("--database", type=Path, default=ROOT / "data/local.db")
    export.add_argument("--media", type=Path, default=ROOT / "data/media")
    export.add_argument("--output", type=Path)
    verify = commands.add_parser("verify", help="Verify all files and SQLite integrity")
    verify.add_argument("--bundle", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "export":
            destination = export_bundle(arguments.database, arguments.media, arguments.output)
            print(f"Verified private bundle: {destination}")
        else:
            manifest = verify_bundle(arguments.bundle)
            print(f"Verified {len(manifest['files'])} files; SQLite integrity OK; sessions removed")
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        parser.exit(1, f"Deployment bundle failed: {error}\n")


if __name__ == "__main__":
    main()
