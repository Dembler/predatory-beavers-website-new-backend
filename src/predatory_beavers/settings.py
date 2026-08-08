from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    name: str = "Predatory Beavers API"
    version: str = "0.1.0"
    env: Literal["dev", "test", "prod"] = "dev"
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    reload: bool = True
    api_prefix: str = "/api/v1"

    database_url: str = "sqlite+aiosqlite:///./data/predatory_beavers.db"
    database_echo: bool = False
    database_busy_timeout_ms: int = Field(default=5000, ge=100, le=60_000)

    media_storage_path: Path = Path("data/media")
    media_max_upload_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    media_max_dimension: int = Field(default=4096, ge=256, le=16_384)
    media_webp_quality: int = Field(default=85, ge=1, le=100)

    asb_enabled: bool = False
    asb_base_url: str = "https://asb.infobasket.su"
    asb_allowed_hosts: list[str] = Field(default_factory=lambda: ["asb.infobasket.su"])
    asb_allowed_competition_ids: list[str] = Field(default_factory=list)
    asb_allowed_team_ids: list[str] = Field(default_factory=list)
    asb_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    asb_max_response_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024)
    asb_max_games: int = Field(default=500, ge=1, le=2000)

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    log_level: str = "INFO"
    log_json: bool = True

    session_cookie_name: str = "pb_session"
    session_ttl_seconds: int = Field(default=43_200, ge=300)
    cookie_secure: bool = False
    csrf_header_name: str = "X-CSRF-Token"
    auth_login_max_attempts: int = Field(default=10, ge=1, le=100)
    auth_login_window_seconds: int = Field(default=60, ge=1, le=3600)
    auth_login_max_concurrency: int = Field(default=4, ge=1, le=32)
    auth_max_active_sessions: int = Field(default=5, ge=1, le=50)

    @field_validator("database_url")
    @classmethod
    def validate_sqlite_database_url(cls, value: str) -> str:
        if not value.startswith("sqlite+aiosqlite:///"):
            raise ValueError("APP_DATABASE_URL must use sqlite+aiosqlite")
        return value

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        parsed_asb_url = urlsplit(self.asb_base_url)
        allowed_hosts = {host.strip().lower() for host in self.asb_allowed_hosts}
        if (
            parsed_asb_url.scheme != "https"
            or not parsed_asb_url.hostname
            or parsed_asb_url.hostname.lower() not in allowed_hosts
            or parsed_asb_url.username is not None
            or parsed_asb_url.password is not None
            or parsed_asb_url.query
            or parsed_asb_url.fragment
            or parsed_asb_url.path not in {"", "/"}
        ):
            raise ValueError("APP_ASB_BASE_URL must be a root HTTPS URL on an allowed host")
        for value in [*self.asb_allowed_competition_ids, *self.asb_allowed_team_ids]:
            if not value.isascii() or not value.isdigit() or int(value) <= 0:
                raise ValueError("ASB allowlists must contain positive numeric IDs")
        if self.asb_enabled and (
            not self.asb_allowed_competition_ids or not self.asb_allowed_team_ids
        ):
            raise ValueError("Enabled ASB integration requires non-empty ID allowlists")

        if self.env != "prod":
            return self
        if not self.cookie_secure:
            raise ValueError("APP_COOKIE_SECURE must be true in production")
        if not self.session_cookie_name.startswith("__Host-"):
            raise ValueError("Production session cookie must use the __Host- prefix")
        if "*" in self.cors_origins:
            raise ValueError("Wildcard CORS origin is not allowed in production")
        if self.reload:
            raise ValueError("APP_RELOAD must be false in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
