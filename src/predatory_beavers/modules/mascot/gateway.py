"""LLM gateway for the Bobrik prototype.

One OpenAI-compatible ``POST /chat/completions`` call per run, owned by the
worker process only. The gateway never leaks provider keys or raw provider
errors into logs or public responses, supports a bounded absolute deadline and
cooperative cancellation (polling), and reports usage only when the provider
actually returned it — never an invented zero.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from predatory_beavers.modules.mascot.config import MascotConfig


class GatewayError(Exception):
    """Normalized, safe gateway error (no raw provider text)."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class GatewayCancelled(GatewayError):
    def __init__(self) -> None:
        super().__init__("cancelled", "Запрос отменён", retryable=False)


class GatewayDeadlineExceeded(GatewayError):
    def __init__(self) -> None:
        super().__init__("deadline_exceeded", "Время ожидания истекло", retryable=True)


class GatewayUnavailable(GatewayError):
    def __init__(self) -> None:
        super().__init__("llm_unavailable", "Бобрик временно недоступен", retryable=True)


class GatewayInvalidResponse(GatewayError):
    def __init__(self) -> None:
        super().__init__(
            "invalid_model_response", "Модель вернула некорректный ответ", retryable=False
        )


@dataclass(frozen=True, slots=True)
class CompletionUsage:
    input_tokens: int | None
    output_tokens: int | None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompletionResult:
    content: str
    usage: CompletionUsage
    model: str


CancelCheck = Callable[[], Awaitable[bool]]


class LLMGateway(Protocol):
    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        deadline: datetime,
        cancel_check: CancelCheck,
        session_id: str = "bobrik-local",
    ) -> CompletionResult: ...


class HttpLLMGateway:
    """OpenAI-compatible chat completions gateway with bounded cancellation."""

    def __init__(
        self,
        config: MascotConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_content_chars: int = 12_000,
    ) -> None:
        self._config = config
        self._transport = transport
        self._max_content_chars = max_content_chars
        self._endpoint = config.llm_base_url.rstrip("/") + "/chat/completions"

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        deadline: datetime,
        cancel_check: CancelCheck,
        session_id: str = "bobrik-local",
    ) -> CompletionResult:
        if datetime.now(UTC) >= deadline:
            raise GatewayDeadlineExceeded
        if await cancel_check():
            raise GatewayCancelled
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "bobrik-local/0.1",
            "x-opencode-session": session_id,
        }
        if self._config.llm_api_key:
            headers["Authorization"] = f"Bearer {self._config.llm_api_key}"
        payload = {
            "model": self._config.llm_chat_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        timeout = httpx.Timeout(max(1.0, (deadline - datetime.now(UTC)).total_seconds() + 5.0))
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False, transport=self._transport
        ) as client:
            request_task = asyncio.create_task(
                client.post(self._endpoint, json=payload, headers=headers)
            )
            try:
                while True:
                    done, _ = await asyncio.wait({request_task}, timeout=0.2)
                    if request_task in done:
                        response = request_task.result()
                        break
                    if await cancel_check():
                        request_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await request_task
                        raise GatewayCancelled
                    if datetime.now(UTC) >= deadline:
                        request_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await request_task
                        raise GatewayDeadlineExceeded
            except (GatewayCancelled, GatewayDeadlineExceeded):
                raise
            except httpx.TimeoutException as exc:
                raise GatewayUnavailable from exc
            except httpx.RequestError as exc:
                raise GatewayUnavailable from exc
            finally:
                if not request_task.done():
                    request_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await request_task

        if response.status_code != 200:
            raise GatewayUnavailable
        if len(response.content) > 128_000:
            raise GatewayInvalidResponse
        try:
            body = json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayInvalidResponse from exc
        return self._parse(body, max_content_chars=self._max_content_chars)

    @staticmethod
    def _parse(body: Any, *, max_content_chars: int = 12_000) -> CompletionResult:
        if not isinstance(body, dict):
            raise GatewayInvalidResponse
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise GatewayInvalidResponse
        first = choices[0]
        if not isinstance(first, dict):
            raise GatewayInvalidResponse
        message = first.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise GatewayInvalidResponse
        content = message["content"]
        if not content.strip() or len(content) > max_content_chars:
            raise GatewayInvalidResponse
        usage = CompletionUsage(input_tokens=None, output_tokens=None)
        usage_raw = body.get("usage")
        if isinstance(usage_raw, dict):
            prompt = usage_raw.get("prompt_tokens")
            completion = usage_raw.get("completion_tokens")
            usage = CompletionUsage(
                input_tokens=prompt if type(prompt) is int and prompt >= 0 else None,
                output_tokens=completion if type(completion) is int and completion >= 0 else None,
                details={
                    key: usage_raw[key]
                    for key in ("cost", "opencode", "total_tokens")
                    if key in usage_raw
                },
            )
        return CompletionResult(
            content=content,
            usage=usage,
            model=str(body.get("model") or "unknown"),
        )
