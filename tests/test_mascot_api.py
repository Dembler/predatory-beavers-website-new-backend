"""API-level tests for the mascot prototype.

Covers the enable/production gate, session creation and reuse, cookie+CSRF
ownership, idempotent run submission, payload-mismatch and active-run
uniqueness, durable SSE replay, and explicit cancellation.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.errors import MascotDisabledError
from predatory_beavers.modules.mascot.models import MascotRun, MascotRunEvent, MascotSession
from predatory_beavers.modules.mascot.router import _ensure_enabled
from predatory_beavers.modules.mascot.security import hash_token
from predatory_beavers.settings import Settings

CSRF_HEADER = "X-CSRF-Token"


def _settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    database_path = (tmp_path / "mascot.sqlite").as_posix()
    return Settings(
        _env_file=None,
        env="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        log_json=False,
        cookie_secure=False,
        mascot_enabled=enabled,
    )


async def _create_schema(settings: Settings) -> None:
    engine = create_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()


def test_ensure_enabled_gates_disabled_and_production() -> None:
    disabled = MascotConfig.from_settings(Settings(_env_file=None, env="test"))
    with pytest.raises(MascotDisabledError):
        _ensure_enabled(disabled)

    production = replace(disabled, env="prod", enabled=True)
    assert production.env == "prod"
    with pytest.raises(MascotDisabledError):
        _ensure_enabled(production)

    enabled = MascotConfig.from_settings(Settings(_env_file=None, env="test", mascot_enabled=True))
    _ensure_enabled(enabled)


def test_routes_return_404_when_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)
    asyncio.run(_create_schema(settings))
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/v1/mascot/session").status_code == 404
        assert client.post("/api/v1/mascot/chat/runs", json={}).status_code == 404
        assert client.get(f"/api/v1/mascot/chat/runs/{uuid4()}/events").status_code == 404


def test_session_create_and_reuse(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    asyncio.run(_create_schema(settings))
    with TestClient(create_app(settings)) as client:
        first = client.post("/api/v1/mascot/session")
        assert first.status_code == 200
        assert first.headers["Cache-Control"] == "no-store"
        body = first.json()
        assert body["session_id"]
        assert body["csrf_token"]
        assert body["expires_at"]
        assert "bobrik_session" in first.cookies

        second = client.post("/api/v1/mascot/session")
        assert second.status_code == 200
        assert second.json()["session_id"] == body["session_id"]
        assert second.json()["csrf_token"] == body["csrf_token"]


def test_chat_run_requires_csrf_and_ownership(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    asyncio.run(_create_schema(settings))
    with TestClient(create_app(settings)) as client:
        session_body = client.post("/api/v1/mascot/session").json()
        session_id = session_body["session_id"]
        csrf = session_body["csrf_token"]

        payload = {
            "client_request_id": str(uuid4()),
            "session_id": session_id,
            "message": "Когда следующий матч?",
        }

        missing_csrf = client.post("/api/v1/mascot/chat/runs", json=payload)
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["code"] == "invalid_csrf_token"

        wrong_session = dict(payload, session_id=str(uuid4()))
        bad_owner = client.post(
            "/api/v1/mascot/chat/runs",
            json=wrong_session,
            headers={CSRF_HEADER: csrf},
        )
        assert bad_owner.status_code == 403
        assert bad_owner.json()["code"] == "session_ownership"

        good = client.post("/api/v1/mascot/chat/runs", json=payload, headers={CSRF_HEADER: csrf})
        assert good.status_code == 202


def test_origin_must_match_allowlist(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    asyncio.run(_create_schema(settings))
    with TestClient(create_app(settings)) as client:
        session_body = client.post("/api/v1/mascot/session").json()
        payload = {
            "client_request_id": str(uuid4()),
            "session_id": session_body["session_id"],
            "message": "Привет",
        }
        response = client.post(
            "/api/v1/mascot/chat/runs",
            json=payload,
            headers={CSRF_HEADER: session_body["csrf_token"], "Origin": "http://evil.example"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "invalid_origin"


def test_idempotent_submit_mismatch_and_active_uniqueness(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    asyncio.run(_create_schema(settings))
    with TestClient(create_app(settings)) as client:
        session_body = client.post("/api/v1/mascot/session").json()
        session_id = session_body["session_id"]
        csrf = session_body["csrf_token"]
        request_id = str(uuid4())
        payload = {
            "client_request_id": request_id,
            "session_id": session_id,
            "message": "Расскажи про историю клуба",
        }
        headers = {CSRF_HEADER: csrf}

        first = client.post("/api/v1/mascot/chat/runs", json=payload, headers=headers)
        assert first.status_code == 202
        run_id = first.json()["run_id"]
        assert first.json()["status"] == "queued"
        assert first.json()["events_url"].endswith("/events")

        replay = client.post("/api/v1/mascot/chat/runs", json=payload, headers=headers)
        assert replay.status_code == 202
        assert replay.json()["run_id"] == run_id

        mismatch = client.post(
            "/api/v1/mascot/chat/runs",
            json=dict(payload, message="Другой вопрос"),
            headers=headers,
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["code"] == "idempotency_payload_mismatch"

        other = client.post(
            "/api/v1/mascot/chat/runs",
            json=dict(payload, client_request_id=str(uuid4())),
            headers=headers,
        )
        assert other.status_code == 409
        assert other.json()["code"] == "active_run_exists"


def test_cancel_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    asyncio.run(_create_schema(settings))
    with TestClient(create_app(settings)) as client:
        session_body = client.post("/api/v1/mascot/session").json()
        csrf = session_body["csrf_token"]
        run = client.post(
            "/api/v1/mascot/chat/runs",
            json={
                "client_request_id": str(uuid4()),
                "session_id": session_body["session_id"],
                "message": "Что с матчами?",
            },
            headers={CSRF_HEADER: csrf},
        ).json()
        run_id = run["run_id"]

        cancel = client.post(
            f"/api/v1/mascot/chat/runs/{run_id}/cancel", headers={CSRF_HEADER: csrf}
        )
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "cancelled"

        again = client.post(
            f"/api/v1/mascot/chat/runs/{run_id}/cancel", headers={CSRF_HEADER: csrf}
        )
        assert again.status_code == 200
        assert again.json()["status"] == "cancelled"


def test_sse_replays_persisted_events_for_terminal_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    asyncio.run(_create_schema(settings))
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    token = "opaque-token-value"
    csrf = "client-readable-csrf"
    now = datetime.now(UTC)

    async def seed() -> tuple[str, str]:
        async with session_factory() as session:
            mascot_session = MascotSession(
                token_hash=hash_token(token),
                csrf_token_hash=hash_token(csrf),
                expires_at=now + timedelta(hours=1),
            )
            session.add(mascot_session)
            await session.flush()
            run = MascotRun(
                session_id=mascot_session.id,
                client_request_id=str(uuid4()),
                request_payload_hash="seed-hash",
                message="Привет",
                status="completed",
                deadline_at=now + timedelta(seconds=20),
                finished_at=now,
            )
            session.add(run)
            await session.flush()
            session.add_all(
                [
                    MascotRunEvent(
                        run_id=run.id, seq=1, event_type="status", payload={"stage": "queued"}
                    ),
                    MascotRunEvent(
                        run_id=run.id,
                        seq=2,
                        event_type="answer",
                        payload={
                            "text": "Привет!",
                            "citations": [],
                            "claims_version": "prototype-v1",
                        },
                    ),
                    MascotRunEvent(
                        run_id=run.id,
                        seq=3,
                        event_type="done",
                        payload={
                            "run_id": str(run.id),
                            "usage": {"input_tokens": 3, "output_tokens": 2},
                        },
                    ),
                ]
            )
            await session.commit()
            return str(mascot_session.id), str(run.id)

    session_id, run_id = asyncio.run(seed())
    asyncio.run(engine.dispose())

    with TestClient(create_app(settings)) as client:
        response = client.get(
            f"/api/v1/mascot/chat/runs/{run_id}/events?after_seq=0",
            cookies={"bobrik_session": token},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = response.text
        assert "id: 1\nevent: status\ndata: " in body
        assert "id: 2\nevent: answer\ndata: " in body
        assert "id: 3\nevent: done\ndata: " in body
        assert "Привет!" in body

        # Ownership: a different cookie cannot read the same run's events.
        intruder = client.get(
            f"/api/v1/mascot/chat/runs/{run_id}/events",
            cookies={"bobrik_session": "someone-else"},
        )
        assert intruder.status_code == 401
