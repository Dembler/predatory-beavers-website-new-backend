"""Typed runtime configuration for the internal Bobrik prototype."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from predatory_beavers.settings import Settings


@dataclass(frozen=True, slots=True)
class MascotConfig:
    enabled: bool
    env: str
    session_cookie: str
    session_ttl_seconds: int
    max_input_chars: int
    deadline_seconds: float
    max_output_tokens: int
    rate_limit_hour: int
    llm_provider: str
    llm_base_url: str
    llm_api_key: str | None = field(repr=False)
    llm_chat_model: str
    llm_editor_model: str
    budget_mode: str
    cookie_secure: bool
    cookie_samesite: Literal["lax", "strict", "none"]
    csrf_header_name: str
    cors_origins: list[str]
    api_prefix: str

    @classmethod
    def from_settings(cls, settings: Settings) -> MascotConfig:
        return cls(
            enabled=settings.mascot_enabled,
            env=settings.env,
            session_cookie=settings.mascot_session_cookie,
            session_ttl_seconds=settings.mascot_session_ttl_hours * 3600,
            max_input_chars=settings.chat_max_input_chars,
            deadline_seconds=settings.chat_deadline_seconds,
            max_output_tokens=settings.chat_max_output_tokens,
            rate_limit_hour=settings.chat_rate_limit_hour,
            llm_provider=settings.llm_provider,
            llm_base_url=settings.llm_base_url,
            llm_api_key=(
                settings.llm_api_key.get_secret_value()
                if settings.llm_api_key is not None
                else None
            ),
            llm_chat_model=settings.llm_chat_model,
            llm_editor_model=settings.llm_editor_model,
            budget_mode=settings.llm_budget_mode,
            cookie_secure=settings.cookie_secure,
            cookie_samesite=settings.cookie_samesite,
            csrf_header_name=settings.csrf_header_name,
            cors_origins=list(settings.cors_origins),
            api_prefix=settings.api_prefix,
        )
