import os
import re
from functools import lru_cache
from ipaddress import ip_network
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        hide_input_in_errors=True,
        extra="ignore",
    )

    name: str = "Predatory Beavers API"
    version: str = "0.1.0"
    env: Literal["dev", "test", "prod"] = "dev"
    e2e_runtime_token: str | None = None
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    reload: bool = True
    trusted_proxy_ips: str = "127.0.0.1"
    api_prefix: str = "/api/v1"

    database_url: str = Field(default="sqlite+aiosqlite:///./data/local.db", repr=False)
    database_echo: bool = False
    database_busy_timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    database_pool_size: int = Field(default=3, ge=1, le=20)
    database_max_overflow: int = Field(default=2, ge=0, le=20)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=120)
    database_connect_timeout_seconds: int = Field(default=15, ge=1, le=120)
    database_schema: str = "public"

    # Internal Bobrik prototype. Public release has separate gates in docs/bobrik.
    mascot_enabled: bool = False
    mascot_session_cookie: str = "bobrik_session"
    mascot_session_ttl_hours: int = Field(default=24, ge=1, le=168)
    chat_max_input_chars: int = Field(default=1000, ge=1, le=1000)
    chat_deadline_seconds: float = Field(default=20, ge=1, le=120)
    chat_max_output_tokens: int = Field(default=500, ge=32, le=2000)
    chat_rate_limit_hour: int = Field(default=20, ge=1, le=100)
    llm_provider: Literal["opencode", "deepseek"] = "opencode"
    llm_base_url: str = "https://opencode.ai/inference/go/openai/v1"
    llm_api_key: SecretStr | None = Field(default=None, repr=False)
    llm_chat_model: str = "deepseek-v4.1-flash"
    llm_editor_model: str = "deepseek-v4.1-flash"
    llm_budget_mode: Literal["unlimited"] = "unlimited"

    @field_validator("llm_base_url")
    @classmethod
    def validate_llm_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("APP_LLM_BASE_URL must be an HTTPS URL without credentials")
        return value.rstrip("/")

    sports_sync_enabled: bool = False
    sports_sync_external_enabled: bool = False
    sports_sync_cache_path: Path = Path("data/sports-sync")
    sports_sync_interval_seconds: int = Field(default=3600, ge=60, le=86_400)
    sports_sync_fiba_interval_seconds: int = Field(default=86_400, ge=3600, le=604_800)
    sports_sync_timeout_seconds: int = Field(default=600, ge=30, le=1800)
    sports_sync_backend_root: Path = Path(".")
    fiba_api_key: str | None = Field(default=None, repr=False)

    media_storage_backend: Literal["local", "supabase"] = "local"
    media_storage_path: Path = Path("data/media")
    media_storage_timeout_seconds: float = Field(default=20.0, ge=1.0, le=60.0)
    supabase_url: str | None = None
    supabase_secret_key: SecretStr | None = Field(default=None, repr=False)
    supabase_storage_bucket: str = "club-media"
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
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    csrf_header_name: str = "X-CSRF-Token"
    auth_login_max_attempts: int = Field(default=10, ge=1, le=100)
    auth_login_window_seconds: int = Field(default=60, ge=1, le=3600)
    auth_login_max_concurrency: int = Field(default=4, ge=1, le=32)
    auth_max_active_sessions: int = Field(default=5, ge=1, le=50)

    @model_validator(mode="after")
    def validate_media_storage(self) -> "Settings":
        if self.media_storage_backend != "supabase":
            return self
        if not self.supabase_url or self.supabase_secret_key is None:
            raise ValueError("Supabase media storage requires its project URL and server secret")
        # Match the REST adapter without constructing a network client during validation.
        parsed = urlsplit(self.supabase_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("APP_SUPABASE_URL must be a root HTTPS URL")
        secret = self.supabase_secret_key.get_secret_value()
        if not secret.startswith("sb_secret_") or any(char.isspace() for char in secret):
            raise ValueError("APP_SUPABASE_SECRET_KEY must be a server-side sb_secret key")
        bucket = self.supabase_storage_bucket
        if (
            not 1 <= len(bucket) <= 63
            or not bucket[0].isascii()
            or not bucket[0].isalnum()
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for char in bucket)
        ):
            raise ValueError("APP_SUPABASE_STORAGE_BUCKET must be a valid bucket name")
        return self

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        value = value.strip()
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                value = "postgresql+psycopg://" + value[len(prefix) :]
                break
        try:
            url = make_url(value)
            valid = (
                url.drivername == "sqlite+aiosqlite" and value.startswith("sqlite+aiosqlite:///")
            ) or (
                url.drivername == "postgresql+psycopg"
                and bool(url.host and url.database and url.username)
            )
            # Force parsing of a malformed port without including the secret URL in errors.
            _ = url.port
        except (ArgumentError, ValueError):
            valid = False
        if not valid:
            raise ValueError(
                "APP_DATABASE_URL must use sqlite+aiosqlite or postgresql+psycopg "
                "with a host, database and username"
            )
        return value

    @field_validator("trusted_proxy_ips")
    @classmethod
    def validate_trusted_proxy_ips(cls, value: str) -> str:
        entries = [entry.strip() for entry in value.split(",")]
        try:
            if not entries or any(not entry for entry in entries):
                raise ValueError
            networks = [ip_network(entry, strict=True) for entry in entries]
            if any(network.prefixlen == 0 for network in networks):
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                "APP_TRUSTED_PROXY_IPS must contain explicit IP addresses or CIDRs, "
                "without wildcards or unrestricted networks"
            ) from exc
        return ",".join(entries)

    @field_validator("database_schema")
    @classmethod
    def validate_database_schema(cls, value: str) -> str:
        if (
            not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value)
            or value.startswith("pg_")
            or value == "information_schema"
        ):
            raise ValueError("APP_DATABASE_SCHEMA must be a non-system lowercase SQL identifier")
        return value

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.e2e_runtime_token and self.env != "test":
            raise ValueError("APP_E2E_RUNTIME_TOKEN is available only in test environments")
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError("APP_COOKIE_SECURE must be true when APP_COOKIE_SAMESITE is none")
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
        database = make_url(self.database_url)
        if database.get_backend_name() == "postgresql" and database.query.get("sslmode") not in {
            "require",
            "verify-ca",
            "verify-full",
        }:
            raise ValueError("Production PostgreSQL requires an explicit TLS sslmode")
        if not self.cookie_secure:
            raise ValueError("APP_COOKIE_SECURE must be true in production")
        if not self.session_cookie_name.startswith("__Host-"):
            raise ValueError("Production session cookie must use the __Host- prefix")
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or "*" in parsed.hostname
                or parsed.path != ""
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Production CORS origins must be exact HTTPS origins")
        if self.reload:
            raise ValueError("APP_RELOAD must be false in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=os.environ.get("APP_CONFIG_FILE", ".env"))
