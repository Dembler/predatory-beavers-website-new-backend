"""Deployment cookie attributes and CSRF protection across SameSite modes."""

import asyncio
import runpy
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.auth.service import PasswordSecurity
from predatory_beavers.settings import Settings


@pytest.mark.parametrize(
    "value",
    ["", "*", "0.0.0.0/0", "::/0", "127.0.0.1,*", "localhost", "https://proxy.test", ","],
)
def test_proxy_trust_rejects_invalid_or_unrestricted_addresses(value: str) -> None:
    with pytest.raises(ValidationError, match="APP_TRUSTED_PROXY_IPS"):
        Settings(_env_file=None, env="test", trusted_proxy_ips=value)


def test_proxy_trust_accepts_only_explicit_ip_addresses_and_networks() -> None:
    assert Settings(_env_file=None).trusted_proxy_ips == "127.0.0.1"
    settings = Settings(_env_file=None, trusted_proxy_ips=" 127.0.0.1, ::1, 172.30.250.0/24 ")
    assert settings.trusted_proxy_ips == "127.0.0.1,::1,172.30.250.0/24"


def test_server_passes_configured_proxy_trust_to_uvicorn() -> None:
    settings = Settings(
        _env_file=None,
        env="test",
        reload=False,
        trusted_proxy_ips="172.30.250.0/24",
    )
    with (
        patch("predatory_beavers.settings.get_settings", return_value=settings),
        patch("uvicorn.run") as serve,
    ):
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "run.py"), run_name="__main__")
    assert serve.call_args.kwargs["forwarded_allow_ips"] == "172.30.250.0/24"
    assert serve.call_args.kwargs["proxy_headers"] is True


@pytest.mark.parametrize(
    ("peer", "expected_scheme", "expected_client"),
    [
        ("172.30.250.2", "https", "198.51.100.9"),
        ("172.30.251.2", "http", "172.30.251.2"),
    ],
)
def test_forwarded_identity_and_https_are_trusted_only_from_proxy_network(
    peer: str, expected_scheme: str, expected_client: str
) -> None:
    settings = Settings(
        _env_file=None,
        env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        trusted_proxy_ips="172.30.250.0/24",
        log_json=False,
    )
    application = create_app(settings)

    @application.get("/connection")
    def connection(request: Request) -> dict[str, str | None]:
        return {
            "scheme": request.url.scheme,
            "client": request.client.host if request.client else None,
        }

    wrapped = ProxyHeadersMiddleware(application, trusted_hosts=settings.trusted_proxy_ips)
    with TestClient(wrapped, client=(peer, 50000)) as client:
        response = client.get(
            "/connection",
            headers={"X-Forwarded-For": "198.51.100.9", "X-Forwarded-Proto": "https"},
        )
    assert response.json() == {"scheme": expected_scheme, "client": expected_client}
    assert ("Strict-Transport-Security" in response.headers) is (expected_scheme == "https")


@pytest.mark.parametrize("environment", ["dev", "test", "prod"])
def test_samesite_none_requires_secure_cookies(environment: str) -> None:
    with pytest.raises(ValidationError, match="APP_COOKIE_SAMESITE is none"):
        Settings(
            _env_file=None,
            env=environment,  # type: ignore[arg-type]
            cookie_samesite="none",
            cookie_secure=False,
        )


def test_production_accepts_explicit_secure_cross_site_cookie_configuration() -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        reload=False,
        cookie_secure=True,
        cookie_samesite="none",
        session_cookie_name="__Host-pb_session",
        cors_origins=["https://club.github.io"],
    )
    assert settings.cookie_samesite == "none"


@pytest.mark.parametrize("same_site", ["lax", "strict", "none"])
def test_login_and_logout_use_configured_cookie_mode_and_keep_csrf_required(
    tmp_path: Path, same_site: Literal["lax", "strict", "none"]
) -> None:
    # TestClient checks HTTP attributes and revocation, not browser third-party policy.
    settings = Settings(
        _env_file=None,
        env="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'cookies.sqlite').as_posix()}",
        media_storage_path=tmp_path / "media",
        sports_sync_enabled=False,
        cookie_secure=True,
        cookie_samesite=same_site,
        session_cookie_name="__Host-pb_session",
        cors_origins=["https://club.github.io"],
        log_json=False,
    )

    async def seed() -> None:
        engine = create_engine(settings)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with create_session_factory(engine)() as session:
                session.add(
                    User(
                        username="deployment-admin",
                        email="deployment-admin@example.test",
                        password_hash=PasswordSecurity().hash_password("cookie-test-password"),
                        role=UserRole.ADMIN,
                        is_active=True,
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed())
    with TestClient(create_app(settings), base_url="https://api.example.test") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "deployment-admin", "password": "cookie-test-password"},
            headers={"Origin": "https://club.github.io"},
        )
        assert login.status_code == 200
        assert login.headers["Access-Control-Allow-Origin"] == "https://club.github.io"
        assert login.headers["Access-Control-Allow-Credentials"] == "true"
        cookie = SimpleCookie(login.headers["Set-Cookie"])[settings.session_cookie_name]
        assert cookie["samesite"] == same_site
        assert cookie["secure"] is True
        assert cookie["httponly"] is True
        assert cookie["path"] == "/"
        assert cookie["domain"] == ""
        assert client.get("/api/v1/auth/me").status_code == 200

        assert client.post("/api/v1/auth/logout").status_code == 403
        assert client.get("/api/v1/auth/me").status_code == 200
        logout = client.post(
            "/api/v1/auth/logout",
            headers={settings.csrf_header_name: login.json()["data"]["csrf_token"]},
        )
        assert logout.status_code == 200
        deleted = SimpleCookie(logout.headers["Set-Cookie"])[settings.session_cookie_name]
        assert deleted["samesite"] == same_site
        assert deleted["secure"] is True
        assert deleted["httponly"] is True
        assert deleted["path"] == "/"
        assert deleted["domain"] == ""
        assert deleted["max-age"] == "0"
        assert client.get("/api/v1/auth/me").status_code == 401
