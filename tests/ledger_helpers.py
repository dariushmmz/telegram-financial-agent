from __future__ import annotations

from datetime import datetime

from receipt_bot.persistence.repositories import GroupRepository, MessageRepository, UserRepository


class LedgerSeed:
    def __init__(self, session, *, chat_id: int, title: str, actor_telegram_id: int) -> None:
        self.session = session
        self.group = GroupRepository(session).add(chat_id, title)
        self.actor = UserRepository(session).add(actor_telegram_id, "حسابدار")
        self._next_message = 1

    def message(
        self,
        text: str,
        occurred_at: datetime,
        *,
        reply_to_message_id: int | None = None,
    ):
        number = self._next_message
        self._next_message += 1
        message, _ = MessageRepository(self.session).add_idempotent(
            group_id=self.group.id,
            telegram_message_id=number,
            telegram_update_id=abs(self.group.telegram_chat_id) * 10_000 + number,
            occurred_at=occurred_at,
            message_type="text",
            sender_user_id=self.actor.id,
            text=text,
            reply_to_message_id=reply_to_message_id,
        )
        return message
