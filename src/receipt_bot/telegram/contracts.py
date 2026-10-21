from __future__ import annotations

from typing import Any, Protocol


class TelegramGateway(Protocol):
    async def get_me(self) -> dict[str, Any]: ...

    async def get_updates(
        self, *, offset: int | None, timeout: int, allowed_updates: list[str]
    ) -> list[dict[str, Any]]: ...

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None, show_alert: bool = False
    ) -> None: ...

    async def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]: ...

    async def download_file(self, file_id: str) -> bytes: ...
