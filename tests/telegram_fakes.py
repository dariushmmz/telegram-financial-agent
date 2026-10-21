from __future__ import annotations

from typing import Any

from receipt_bot.telegram.access import TelegramAccessPolicy
from receipt_bot.telegram.api import TelegramAPIError

TEST_ACCESS_POLICY = TelegramAccessPolicy(
    frozenset({"user_1", "user_2", "user_7", "user_10"})
)


class FakeTelegramGateway:
    def __init__(self) -> None:
        self.members: dict[tuple[int, int], dict[str, Any]] = {}
        self.downloads: dict[str, bytes | list[bytes | Exception] | Exception] = {}
        self.sent: list[dict[str, Any]] = []
        self.answered_callbacks: list[dict[str, Any]] = []
        self.update_batches: list[list[dict[str, Any]]] = []
        self.member_checks: list[tuple[int, int]] = []
        self.download_requests: list[str] = []

    async def get_me(self):
        return {"id": 999, "is_bot": True, "username": "test_receipt_bot"}

    def set_member(
        self,
        chat_id: int,
        user_id: int,
        status: str,
        *,
        is_member: bool | None = None,
        profile: dict[str, Any] | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "status": status,
            "user": profile or {"id": user_id, "first_name": f"user-{user_id}"},
        }
        if is_member is not None:
            item["is_member"] = is_member
        self.members[(chat_id, user_id)] = item

    async def get_updates(self, *, offset, timeout, allowed_updates):
        return self.update_batches.pop(0) if self.update_batches else []

    async def send_message(self, chat_id, text, *, reply_markup=None):
        item = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        self.sent.append(item)
        return {"message_id": len(self.sent), **item}

    async def answer_callback_query(self, callback_query_id, *, text=None, show_alert=False):
        self.answered_callbacks.append(
            {"id": callback_query_id, "text": text, "show_alert": show_alert}
        )

    async def get_chat_member(self, chat_id, user_id):
        self.member_checks.append((chat_id, user_id))
        return self.members.get(
            (chat_id, user_id),
            {"status": "left", "user": {"id": user_id, "first_name": f"user-{user_id}"}},
        )

    async def download_file(self, file_id):
        self.download_requests.append(file_id)
        event = self.downloads.get(file_id)
        if isinstance(event, list):
            event = event.pop(0)
        if isinstance(event, Exception):
            raise event
        if not isinstance(event, bytes):
            raise TelegramAPIError("file_unavailable", retryable=True)
        return event
