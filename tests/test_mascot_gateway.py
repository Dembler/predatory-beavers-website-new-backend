"""Gateway contract checks with an in-memory transport and no paid requests."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.gateway import (
    GatewayCancelled,
    GatewayDeadlineExceeded,
    GatewayUnavailable,
    HttpLLMGateway,
)
from predatory_beavers.settings import Settings


def _config() -> MascotConfig:
    return MascotConfig.from_settings(
        Settings(
            _env_file=None,
            env="test",
            mascot_enabled=True,
            llm_base_url="https://opencode.ai/inference/go/openai/v1",
            llm_chat_model="deepseek-v4.1-flash",
            llm_api_key="unit-test-provider-key",
        )
    )


async def _not_cancelled() -> bool:
    return False


@pytest.mark.asyncio
async def test_go_request_contract_and_usage_metadata_are_preserved() -> None:
    requests: list[httpx.Request] = []
    details = {
        "cost": {"total": 0.00004},
        "opencode": {"billing_source": "go", "remaining": 145},
        "total_tokens": 19,
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4.1-flash",
                "choices": [{"message": {"content": '{"text":"Ответ","source_ids":[]}'}}],
                "usage": {"prompt_tokens": 14, "completion_tokens": 5, **details},
            },
        )

    gateway = HttpLLMGateway(_config(), transport=httpx.MockTransport(respond))
    for _ in range(2):
        result = await gateway.complete(
            system="System rules",
            user="Question",
            max_tokens=500,
            temperature=0.2,
            deadline=datetime.now(UTC) + timedelta(seconds=3),
            cancel_check=_not_cancelled,
            session_id="same-anonymous-conversation",
        )
        assert result.model == "deepseek-v4.1-flash"
        assert result.usage.input_tokens == 14
        assert result.usage.output_tokens == 5
        assert result.usage.details == details
    assert len(requests) == 2
    for request in requests:
        assert request.method == "POST"
        assert str(request.url) == ("https://opencode.ai/inference/go/openai/v1/chat/completions")
        assert request.headers["x-opencode-session"] == "same-anonymous-conversation"
        assert request.headers["user-agent"] == "bobrik-local/0.1"
        assert request.headers["authorization"] == "Bearer unit-test-provider-key"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-v4.1-flash"
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == 500
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["messages"] == [
            {"role": "system", "content": "System rules"},
            {"role": "user", "content": "Question"},
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["cancel", "deadline", "external_cancel"])
async def test_pending_request_is_cancelled_once_without_retry(stop_reason: str) -> None:
    started = asyncio.Event()
    transport_cancelled = asyncio.Event()
    cancel_requested = asyncio.Event()
    calls = 0

    async def pending(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("Pending transport must be cancelled")
        except asyncio.CancelledError:
            transport_cancelled.set()
            raise

    async def check_cancel() -> bool:
        return cancel_requested.is_set()

    gateway = HttpLLMGateway(_config(), transport=httpx.MockTransport(pending))
    deadline = datetime.now(UTC) + timedelta(seconds=0.05 if stop_reason == "deadline" else 5)
    task = asyncio.create_task(
        gateway.complete(
            system="System rules",
            user="Question",
            max_tokens=500,
            temperature=0.2,
            deadline=deadline,
            cancel_check=check_cancel,
            session_id="cancel-test",
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        if stop_reason == "cancel":
            cancel_requested.set()
            expected_error = GatewayCancelled
        elif stop_reason == "deadline":
            expected_error = GatewayDeadlineExceeded
        else:
            task.cancel()
            expected_error = asyncio.CancelledError
        with pytest.raises(expected_error):
            await asyncio.wait_for(task, timeout=2)
        assert transport_cancelled.is_set()
        assert calls == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_expired_deadline_makes_no_provider_request() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    gateway = HttpLLMGateway(_config(), transport=httpx.MockTransport(respond))
    with pytest.raises(GatewayDeadlineExceeded):
        await gateway.complete(
            system="System rules",
            user="Question",
            max_tokens=500,
            temperature=0.2,
            deadline=datetime.now(UTC) - timedelta(seconds=1),
            cancel_check=_not_cancelled,
        )
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [402, 429, 503])
async def test_provider_error_is_normalized_without_retry(status: int) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="private provider error detail")

    gateway = HttpLLMGateway(_config(), transport=httpx.MockTransport(respond))
    with pytest.raises(GatewayUnavailable) as error:
        await gateway.complete(
            system="System rules",
            user="Question",
            max_tokens=500,
            temperature=0.2,
            deadline=datetime.now(UTC) + timedelta(seconds=3),
            cancel_check=_not_cancelled,
        )
    assert calls == 1
    assert "private provider error detail" not in str(error.value)
