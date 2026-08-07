from typing import Protocol


class TelegramClient(Protocol):
    """Port implemented by the standalone notification worker later."""

    async def send_message(self, *, chat_id: int, text: str) -> str: ...
