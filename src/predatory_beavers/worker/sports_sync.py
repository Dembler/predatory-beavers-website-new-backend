"""Periodic official sports imports, independent of the HTTP request lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)
MAX_REPORT_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class SportsSyncConfig:
    backend_root: Path
    database: Path
    interval_seconds: float = 3600
    timeout_seconds: float = 600
    state_path: Path | None = None

    def __post_init__(self) -> None:
        root = self.backend_root.resolve()
        object.__setattr__(self, "backend_root", root)
        database = self.database
        if not database.is_absolute():
            database = root / database
        object.__setattr__(self, "database", database.resolve())
        state = self.state_path or self.database.parent / "sports-sync" / "status.json"
        if not state.is_absolute():
            state = root / state
        object.__setattr__(self, "state_path", state.resolve())
        if self.interval_seconds <= 0 or self.timeout_seconds <= 0:
            raise ValueError("Sports sync intervals and timeouts must be positive")

    @property
    def lock_path(self) -> Path:
        return self.database.with_name(self.database.name + ".sports-sync.lock")


class ProcessLock:
    """An OS-owned lock: crashes release it and the inode is never deleted."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> bool:
        if self._file is not None:
            raise RuntimeError("Lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            # Windows byte-range locking needs an actual byte at offset zero.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        self._file = handle
        return True

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_report(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            contents = handle.read(MAX_REPORT_BYTES + 1)
        if len(contents) > MAX_REPORT_BYTES:
            return {}
        value = json.loads(contents)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_status(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(status, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _merge_sources(previous: Any, current: dict[str, Any]) -> dict[str, Any]:
    """Retain the last good update time when only one upstream site is unavailable."""
    sources = dict(previous) if isinstance(previous, dict) else {}
    for name, result in current.items():
        if not isinstance(result, dict):
            continue
        older = sources.get(name, {})
        merged = dict(result)
        prior_attempt = older.get("last_attempt_at") if isinstance(older, dict) else None
        merged["last_attempt_at"] = (
            result.get("started_at") or result.get("last_attempt_at") or prior_attempt or _now()
        )
        if result.get("status") in {"success", "succeeded", "ok"}:
            merged.setdefault("last_success_at", merged["last_attempt_at"])
        elif isinstance(older, dict):
            merged.setdefault("last_success_at", older.get("last_success_at"))
        sources[name] = merged
    return sources


async def _drain(reader: asyncio.StreamReader | None) -> None:
    """Discard output in bounded chunks; imported sites cannot grow worker logs."""
    if reader is not None:
        while await reader.read(64 * 1024):
            pass


async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        # terminate() alone leaves the refresh script's subprocesses running.
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    await process.wait()


class SportsSyncWorker:
    def __init__(self, config: SportsSyncConfig) -> None:
        self.config = config

    async def run_once(self) -> bool:
        """Return success; another active importer is skipped without altering its state."""
        config = self.config
        lock = ProcessLock(config.lock_path)
        if not lock.acquire():
            logger.info("Sports refresh skipped: another worker owns the database lock")
            return False
        try:
            return await self._run_locked()
        finally:
            lock.release()

    async def _run_locked(self) -> bool:
        config = self.config
        assert config.state_path is not None
        previous = _read_report(config.state_path)
        status: dict[str, Any] = {
            "state": "running",
            "attempt_started_at": _now(),
            "attempt_finished_at": None,
            "last_success_at": previous.get("last_success_at"),
            "last_error": None,
            "sources": previous.get("sources", {}),
            "next_attempt_at": None,
        }
        _write_status(config.state_path, status)
        # Each attempt has its own file; a failed import cannot reuse a stale report.
        with tempfile.TemporaryDirectory(
            prefix="sports-sync-", dir=config.state_path.parent
        ) as tmp:
            report_path = Path(tmp) / "result.json"
            process: asyncio.subprocess.Process | None = None
            drains: list[asyncio.Task[None]] = []
            cancelled = False
            try:
                options: dict[str, Any] = {}
                if sys.platform == "win32":
                    options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    options["start_new_session"] = True
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(config.backend_root / "scripts" / "refresh_sports.py"),
                    "--database",
                    str(config.database),
                    "--status-json",
                    str(report_path),
                    "--lock-owned",
                    cwd=config.backend_root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **options,
                )
                drains = [
                    asyncio.create_task(_drain(process.stdout)),
                    asyncio.create_task(_drain(process.stderr)),
                ]
                await asyncio.wait_for(process.wait(), config.timeout_seconds)
                status["returncode"] = process.returncode
                if process.returncode == 0:
                    status["state"] = "succeeded"
                    status["last_success_at"] = _now()
                else:
                    status["last_error"] = f"Refresh exited with status {process.returncode}"
            except TimeoutError:
                status["last_error"] = "Refresh exceeded its configured timeout"
            except asyncio.CancelledError:
                status["last_error"] = "Refresh interrupted during worker shutdown"
                cancelled = True
            except OSError as error:
                # Do not copy commands, environment variables or child output into public state.
                status["last_error"] = f"Unable to start refresh ({type(error).__name__})"
            finally:
                if process is not None and process.returncode is None:
                    await _terminate_tree(process)
                await asyncio.gather(*drains, return_exceptions=True)
                report = _read_report(report_path)
                if isinstance(report.get("sources"), dict):
                    status["sources"] = _merge_sources(status["sources"], report["sources"])
                if status["state"] == "succeeded" and report.get("status") == "partial":
                    status["state"] = "partial"
                    status["last_success_at"] = previous.get("last_success_at")
                    status["last_error"] = "Some official sources could not be refreshed"
                elif status["state"] == "succeeded" and report.get("status") != "succeeded":
                    status["state"] = "failed"
                    status["last_success_at"] = previous.get("last_success_at")
                    status["last_error"] = "Refresh did not return a successful source report"
                elif status["state"] != "succeeded":
                    status["state"] = "failed"
                status["attempt_finished_at"] = _now()
                status["next_attempt_at"] = (
                    datetime.now(UTC) + timedelta(seconds=config.interval_seconds)
                ).isoformat()
                _write_status(config.state_path, status)
            if cancelled:
                raise asyncio.CancelledError
        succeeded = bool(status["state"] in {"succeeded", "partial"})
        if succeeded:
            logger.info("Scheduled sports refresh finished")
        else:
            logger.warning("Scheduled sports refresh failed: %s", status["last_error"])
        return succeeded

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A transient filesystem failure must not silently stop future attempts.
                logger.error("Sports worker attempt failed (%s)", type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), self.config.interval_seconds)
            except TimeoutError:
                pass


async def run_sports_sync(
    config: SportsSyncConfig, stop_event: asyncio.Event | None = None
) -> None:
    await SportsSyncWorker(config).run(stop_event)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backend-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--interval-seconds", type=float, default=3600)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.interval_seconds < 60 or args.timeout_seconds <= 0:
        parser.error("interval must be at least 60 seconds and timeout must be positive")
    config = SportsSyncConfig(
        backend_root=args.backend_root,
        database=args.database,
        interval_seconds=args.interval_seconds,
        timeout_seconds=args.timeout_seconds,
        state_path=args.state_path,
    )
    logging.basicConfig(level=logging.INFO)
    try:
        if args.once:
            success = asyncio.run(SportsSyncWorker(config).run_once())
            if not success:
                raise SystemExit(1)
        else:
            asyncio.run(run_sports_sync(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
