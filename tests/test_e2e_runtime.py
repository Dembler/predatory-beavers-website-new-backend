from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from predatory_beavers.api.main import create_app
from predatory_beavers.settings import Settings


def test_e2e_runtime_requires_test_mode_token_and_isolated_database(tmp_path: Path) -> None:
    database = tmp_path / "e2e.db"
    settings = Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
        e2e_runtime_token="test-runtime-token",
        media_storage_path=tmp_path / "media",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/e2e").status_code == 404
        assert (
            client.get("/health/e2e", headers={"X-E2E-Runtime-Token": "wrong"}).status_code == 404
        )
        response = client.get("/health/e2e", headers={"X-E2E-Runtime-Token": "test-runtime-token"})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["databasePath"] == str(database.resolve())
        assert response.json()["isolated"] is True
    settings.database_url = f"sqlite+aiosqlite:///{(tmp_path / 'production.db').as_posix()}"
    with TestClient(create_app(settings)) as client:
        assert (
            client.get(
                "/health/e2e", headers={"X-E2E-Runtime-Token": "test-runtime-token"}
            ).status_code
            == 404
        )
    with pytest.raises(ValidationError, match="only in test"):
        Settings(env="dev", e2e_runtime_token="unexpected")
