"""The automatic importer owns one database, retries failures and stops cleanly."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from predatory_beavers.worker import sports_sync
from predatory_beavers.worker.sports_sync import (
    ProcessLock,
    SportsSyncConfig,
    SportsSyncWorker,
)


@pytest.fixture
def config(tmp_path):
    (tmp_path / "scripts").mkdir()
    return SportsSyncConfig(
        backend_root=tmp_path,
        database=Path("data/intended.db"),
        state_path=Path("data/status.json"),
        timeout_seconds=10,
    )


def refresh_script(config, body):
    script = config.backend_root / "scripts" / "refresh_sports.py"
    script.write_text(
        "import argparse, json, os, pathlib, sys, time\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--database', required=True)\n"
        "p.add_argument('--status-json', required=True)\n"
        "p.add_argument('--lock-owned', action='store_true', required=True)\n"
        "args = p.parse_args()\n" + body,
        encoding="utf-8",
    )


def read_status(config):
    return json.loads(config.state_path.read_text(encoding="utf-8"))


async def test_refresh_uses_absolute_target_current_python_and_persists_source_results(config):
    refresh_script(
        config,
        "pathlib.Path('observed.json').write_text(json.dumps({"
        "'database': args.database, 'cwd': os.getcwd(), 'python': sys.executable}))\n"
        "pathlib.Path(args.status_json).write_text(json.dumps({'status': 'succeeded', 'sources': {"
        "'asb': {'status': 'succeeded', 'games': 3},"
        "'fiba': {'status': 'succeeded', 'games': 2}}}))\n",
    )
    assert await SportsSyncWorker(config).run_once()
    observed = json.loads((config.backend_root / "observed.json").read_text())
    assert Path(observed["database"]) == config.database
    assert config.database.is_absolute()
    assert Path(observed["cwd"]) == config.backend_root
    assert Path(observed["python"]) == Path(sys.executable)
    status = read_status(config)
    assert status["state"] == "succeeded"
    assert status["last_success_at"]
    assert status["last_error"] is None
    assert status["attempt_started_at"] <= status["attempt_finished_at"]
    assert status["next_attempt_at"] > status["attempt_finished_at"]
    assert status["sources"]["asb"]["games"] == 3
    assert status["sources"]["fiba"]["games"] == 2
    assert status["sources"]["asb"]["last_success_at"]


async def test_failure_keeps_last_success_and_does_not_expose_or_buffer_child_output(config):
    config.state_path.parent.mkdir(parents=True)
    config.state_path.write_text(json.dumps({"last_success_at": "2026-11-14T10:00:00Z"}))
    refresh_script(
        config,
        "print('credential-that-must-not-be-stored' * 100000, flush=True)\n"
        "print('private-stderr' * 100000, file=sys.stderr, flush=True)\n"
        "pathlib.Path(args.status_json).write_text(json.dumps({'sources': {"
        "'asb': {'status': 'failed', 'error': 'upstream_unavailable'}}}))\n"
        "sys.exit(7)\n",
    )
    assert not await SportsSyncWorker(config).run_once()
    status = read_status(config)
    assert status["state"] == "failed"
    assert status["last_success_at"] == "2026-11-14T10:00:00Z"
    assert status["returncode"] == 7
    assert status["sources"]["asb"]["status"] == "failed"
    assert "credential" not in config.state_path.read_text()
    assert "private-stderr" not in config.state_path.read_text()
    assert config.state_path.stat().st_size < 2048


async def test_partial_refresh_remains_usable_without_claiming_all_sources_succeeded(config):
    config.state_path.parent.mkdir(parents=True)
    config.state_path.write_text(json.dumps({"last_success_at": "2026-11-14T10:00:00Z"}))
    refresh_script(
        config,
        "pathlib.Path(args.status_json).write_text(json.dumps({"
        "'status': 'partial', 'sources': {"
        "'asb_games_men': {'status': 'succeeded', 'result': {'games': 3}},"
        "'fiba3x3': {'status': 'auth_required', 'last_success_at': None}}}))\n",
    )
    assert await SportsSyncWorker(config).run_once()
    status = read_status(config)
    assert status["state"] == "partial"
    assert status["last_success_at"] == "2026-11-14T10:00:00Z"
    assert status["sources"]["asb_games_men"]["last_success_at"]
    assert status["sources"]["fiba3x3"]["last_success_at"] is None
    assert status["next_attempt_at"] > status["attempt_finished_at"]
    assert status["last_error"] == "Some official sources could not be refreshed"


async def test_success_exit_without_source_report_is_not_reported_as_a_success(config):
    refresh_script(config, "pass\n")
    assert not await SportsSyncWorker(config).run_once()
    status = read_status(config)
    assert status["state"] == "failed"
    assert status["last_success_at"] is None
    assert status["last_error"] == "Refresh did not return a successful source report"


async def test_active_worker_lock_skips_attempt_and_preserves_its_status(config):
    config.state_path.parent.mkdir(parents=True)
    original = '{"state":"running","attempt_started_at":"already-running"}'
    config.state_path.write_text(original)
    refresh_script(config, "raise AssertionError('must not start')\n")
    lock = ProcessLock(config.lock_path)
    assert lock.acquire()
    try:
        assert not await SportsSyncWorker(config).run_once()
    finally:
        lock.release()
    assert config.state_path.read_text() == original
    assert config.lock_path.exists()  # Never unlink an inode another waiter could own.


def test_database_lock_is_exclusive_across_processes_and_released_after_exit(config):
    code = (
        "from pathlib import Path; "
        "from predatory_beavers.worker.sports_sync import ProcessLock; "
        f"lock = ProcessLock(Path({str(config.lock_path)!r})); "
        "print(lock.acquire(), flush=True)"
    )
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")
    lock = ProcessLock(config.lock_path)
    assert lock.acquire()
    try:
        blocked = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            env=environment,
        )
        assert blocked.stdout.strip() == "False"
    finally:
        lock.release()
    acquired = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env=environment,
    )
    assert acquired.stdout.strip() == "True"
    assert lock.acquire()  # OS releases the successful child's lock on exit.
    lock.release()


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_shutdown_terminates_child_before_releasing_lock(
    config, monkeypatch, cancel
):
    config = SportsSyncConfig(
        backend_root=config.backend_root,
        database=config.database,
        state_path=config.state_path,
        timeout_seconds=0.1 if not cancel else 10,
    )
    refresh_script(config, "time.sleep(30)\n")
    original_terminate = sports_sync._terminate_tree
    terminated = []

    async def observe_termination(process):
        await original_terminate(process)
        terminated.append(process.returncode)
        lock = ProcessLock(config.lock_path)
        assert not lock.acquire()  # Remain exclusive until the process has exited.

    monkeypatch.setattr(sports_sync, "_terminate_tree", observe_termination)
    task = asyncio.create_task(SportsSyncWorker(config).run_once())
    if cancel:
        for _ in range(200):
            if config.state_path.exists():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert not await task
    assert len(terminated) == 1
    assert terminated[0] is not None
    status = read_status(config)
    assert status["state"] == "failed"
    assert status["attempt_finished_at"]
    assert "shutdown" in status["last_error"] if cancel else "timeout" in status["last_error"]
    lock = ProcessLock(config.lock_path)
    assert lock.acquire()
    lock.release()


async def test_loop_retries_a_failed_attempt_and_stops_without_an_extra_import(config, monkeypatch):
    stop = asyncio.Event()
    calls = []
    worker = SportsSyncWorker(
        SportsSyncConfig(
            backend_root=config.backend_root, database=config.database, interval_seconds=0.01
        )
    )

    async def attempt():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("transient state file failure")
        stop.set()
        return True

    monkeypatch.setattr(worker, "run_once", attempt)
    await asyncio.wait_for(worker.run(stop), timeout=2)
    assert len(calls) == 2


def test_report_reader_rejects_invalid_or_oversize_data(tmp_path):
    report = tmp_path / "result.json"
    report.write_text("not valid json")
    assert sports_sync._read_report(report) == {}
    report.write_text(json.dumps({"sources": "x" * sports_sync.MAX_REPORT_BYTES}))
    assert sports_sync._read_report(report) == {}


def test_source_failure_retains_its_last_success_while_other_source_advances():
    previous = {
        "asb": {"status": "success", "last_success_at": "2026-11-14T10:00:00Z"},
        "fiba": {"status": "error", "last_success_at": "2026-11-13T10:00:00Z"},
    }
    merged = sports_sync._merge_sources(
        previous,
        {
            "asb": {"status": "error", "error": "upstream_unavailable"},
            "fiba": {"status": "success", "counts": {"games": 4}},
        },
    )
    assert merged["asb"]["last_success_at"] == "2026-11-14T10:00:00Z"
    assert merged["asb"]["status"] == "error"
    assert merged["asb"]["last_attempt_at"] > previous["asb"]["last_success_at"]
    assert merged["fiba"]["last_success_at"] > previous["fiba"]["last_success_at"]
    assert merged["fiba"]["counts"]["games"] == 4
    assert previous["asb"]["status"] == "success"  # Caller state is not mutated.


def test_skipped_source_keeps_original_attempt_time_and_actual_source_started_at():
    source = {
        "status": "succeeded",
        "started_at": "2026-11-14T09:00:00Z",
        "finished_at": "2026-11-14T09:01:00Z",
        "last_success_at": "2026-11-14T09:01:00Z",
        "next_attempt_at": "2026-11-15T09:01:00Z",
    }
    previous = {"fiba3x3": {**source, "last_attempt_at": source["started_at"]}}
    merged = sports_sync._merge_sources(previous, {"fiba3x3": source})
    assert merged["fiba3x3"] == previous["fiba3x3"]
    assert (
        sports_sync._merge_sources({}, {"fiba3x3": source})["fiba3x3"]["last_attempt_at"]
        == source["started_at"]
    )


def test_status_replacement_is_atomic_and_leaves_no_temporary_files(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    path.write_text('{"state":"previous"}')
    original_replace = os.replace

    def observe_replace(source, destination):
        assert json.loads(path.read_text()) == {"state": "previous"}
        assert json.loads(Path(source).read_text()) == {"state": "next"}
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", observe_replace)
    sports_sync._write_status(path, {"state": "next"})
    assert json.loads(path.read_text()) == {"state": "next"}
    assert list(tmp_path.iterdir()) == [path]
