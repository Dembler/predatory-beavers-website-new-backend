from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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

    database_url: str = (
        "postgresql+asyncpg://predatory_beavers:predatory_beavers@localhost:5432/predatory_beavers"
    )
    database_echo: bool = False

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

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
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
