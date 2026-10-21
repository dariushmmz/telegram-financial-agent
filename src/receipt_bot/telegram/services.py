from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import quote

from receipt_bot.domain.time import utc_now
from receipt_bot.persistence.database import Database
from receipt_bot.persistence.models import InteractionSession, TelegramGroup, TelegramUser
from receipt_bot.persistence.repositories import (
    GroupRepository,
    HistoryRepository,
    InteractionRepository,
    MembershipRepository,
    UserRepository,
)
from receipt_bot.telegram.contracts import TelegramGateway


class AccessDenied(PermissionError):
    pass


def telegram_display_name(user: dict[str, Any]) -> str:
    parts = [str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip()]
    name = " ".join(part for part in parts if part)
    return name or str(user.get("username") or user.get("id") or "کاربر")


class AuthorizationService:
    ADMIN_STATUSES = {"creator", "administrator"}
    PRESENT_STATUSES = {"creator", "administrator", "member"}

    def __init__(self, database: Database, gateway: TelegramGateway) -> None:
        self.database = database
        self.gateway = gateway

    async def is_current_admin(self, telegram_chat_id: int, telegram_user_id: int) -> bool:
        member = await self.gateway.get_chat_member(telegram_chat_id, telegram_user_id)
        return member.get("status") in self.ADMIN_STATUSES

    async def require_admin(self, telegram_chat_id: int, telegram_user_id: int) -> None:
        if not await self.is_current_admin(telegram_chat_id, telegram_user_id):
            raise AccessDenied("current Telegram administrator status is required")

    async def require_reviewer(
        self, group: TelegramGroup, user: TelegramUser
    ) -> None:
        member = await self.gateway.get_chat_member(group.telegram_chat_id, user.telegram_user_id)
        status = member.get("status")
        if status in self.ADMIN_STATUSES:
            return
        is_present = status in self.PRESENT_STATUSES or (
            status == "restricted" and member.get("is_member") is True
        )
        if not is_present:
            raise AccessDenied("user is no longer a current group member")
        with self.database.session() as session:
            if not MembershipRepository(session, group.id).is_active_accountant(user.id):
                raise AccessDenied("active accountant role is required")


class SettingsService:
    def __init__(self, database: Database, authorization: AuthorizationService) -> None:
        self.database = database
        self.authorization = authorization

    async def activate_group(
        self, *, telegram_chat_id: int, telegram_user_id: int
    ) -> tuple[TelegramGroup, bool]:
        await self.authorization.require_admin(telegram_chat_id, telegram_user_id)
        with self.database.session() as session:
            group = GroupRepository(session).get_by_chat_id(telegram_chat_id)
            user = UserRepository(session).get_by_telegram_id(telegram_user_id)
            if group is None or user is None:
                raise LookupError("group or user has not been observed")
            changed = not group.active
            if changed:
                GroupRepository(session).set_active(group, True)
                HistoryRepository(session, group.id).append_audit(
                    action="group.activated",
                    target_type="telegram_group",
                    target_id=group.id,
                    actor_user_id=user.id,
                )
            return group, changed

    async def set_retention_days(
        self,
        *,
        telegram_chat_id: int,
        telegram_user_id: int,
        days: int,
    ) -> TelegramGroup:
        if days not in {90, 183, 365}:
            raise ValueError("unsupported retention option")
        await self.authorization.require_admin(telegram_chat_id, telegram_user_id)
        with self.database.session() as session:
            groups = GroupRepository(session)
            users = UserRepository(session)
            group = groups.get_by_chat_id(telegram_chat_id)
            user = users.get_by_telegram_id(telegram_user_id)
            if group is None or user is None or not group.active:
                raise LookupError("active group or actor does not exist")
            before = group.retention_days
            if before != days:
                group.retention_days = days
                group.settings_version += 1
                session.flush()
                HistoryRepository(session, group.id).append_audit(
                    action="group.retention_changed",
                    target_type="telegram_group",
                    target_id=group.id,
                    actor_user_id=user.id,
                    metadata={"before_days": before, "after_days": days},
                )
            return group

    async def set_accountant(
        self,
        *,
        telegram_chat_id: int,
        actor_telegram_user_id: int,
        target_telegram_user_id: int,
        active: bool,
        target_profile: dict[str, Any] | None = None,
    ) -> TelegramUser:
        await self.authorization.require_admin(telegram_chat_id, actor_telegram_user_id)
        target_member = await self.authorization.gateway.get_chat_member(
            telegram_chat_id, target_telegram_user_id
        )
        status = target_member.get("status")
        present = status in AuthorizationService.PRESENT_STATUSES or (
            status == "restricted" and target_member.get("is_member") is True
        )
        if active and not present:
            raise AccessDenied("target reviewer is not a current group member")
        profile = target_profile or target_member.get("user") or {"id": target_telegram_user_id}
        with self.database.session() as session:
            group = GroupRepository(session).get_by_chat_id(telegram_chat_id)
            actor = UserRepository(session).get_by_telegram_id(actor_telegram_user_id)
            if group is None or actor is None or not group.active:
                raise LookupError("active group or actor does not exist")
            target, _ = UserRepository(session).get_or_create(
                target_telegram_user_id,
                telegram_display_name(profile),
                profile.get("username"),
            )
            MembershipRepository(session, group.id).set_accountant(
                user_id=target.id,
                active=active,
                actor_user_id=actor.id,
            )
            HistoryRepository(session, group.id).append_audit(
                action="accountant.granted" if active else "accountant.revoked",
                target_type="telegram_user",
                target_id=target.id,
                actor_user_id=actor.id,
                metadata={"active": active},
            )
            return target


class InteractionService:
    def __init__(self, database: Database, *, ttl_minutes: int = 15) -> None:
        self.database = database
        self.ttl = timedelta(minutes=ttl_minutes)

    def create(
        self,
        *,
        group_id: int,
        user_id: int,
        workflow: str,
        state: dict[str, Any] | None = None,
    ) -> InteractionSession:
        with self.database.session() as session:
            return InteractionRepository(session).create(
                group_id=group_id,
                initiating_user_id=user_id,
                workflow=workflow,
                state=state or {},
                expires_at=utc_now() + self.ttl,
            )

    def resolve(
        self, token: str, *, user_id: int, group_id: int | None = None
    ) -> InteractionSession | None:
        with self.database.session() as session:
            return InteractionRepository(session).resolve(
                token, initiating_user_id=user_id, group_id=group_id
            )

    def consume(self, token: str, *, user_id: int, group_id: int | None = None) -> bool:
        with self.database.session() as session:
            repository = InteractionRepository(session)
            item = repository.resolve(token, initiating_user_id=user_id, group_id=group_id)
            if item is None:
                return False
            repository.consume(item)
            return True

    def update_state(
        self,
        token: str,
        *,
        user_id: int,
        group_id: int,
        state: dict[str, Any],
    ) -> bool:
        with self.database.session() as session:
            repository = InteractionRepository(session)
            item = repository.resolve(token, initiating_user_id=user_id, group_id=group_id)
            if item is None:
                return False
            repository.update_state(item, state)
            return True

    def find_request(
        self,
        *,
        group_id: int,
        user_id: int,
        request_id: int,
    ) -> InteractionSession | None:
        with self.database.session() as session:
            return InteractionRepository(session).find_request(
                group_id=group_id,
                initiating_user_id=user_id,
                workflow="settings_reviewer",
                request_id=request_id,
            )

    def find_prompt(
        self,
        *,
        group_id: int,
        user_id: int,
        prompt_message_id: int,
    ) -> InteractionSession | None:
        with self.database.session() as session:
            return InteractionRepository(session).find_prompt(
                group_id=group_id,
                initiating_user_id=user_id,
                prompt_message_id=prompt_message_id,
            )

    def find_pending_financial_input(
        self,
        *,
        group_id: int,
        user_id: int,
    ) -> InteractionSession | None:
        with self.database.session() as session:
            return InteractionRepository(session).find_pending_financial_input(
                group_id=group_id,
                initiating_user_id=user_id,
            )

    def consume_pending_financial_inputs(
        self,
        *,
        group_id: int,
        user_id: int,
    ) -> int:
        with self.database.session() as session:
            return InteractionRepository(session).consume_pending_financial_inputs(
                group_id=group_id,
                initiating_user_id=user_id,
            )

    @staticmethod
    def callback_data(token: str, action: str) -> str:
        value = f"i:{token}:{action}"
        if len(value.encode("utf-8")) > 64:
            raise ValueError("callback data exceeds Telegram's 64-byte limit")
        return value

    @staticmethod
    def private_url(bot_username: str, token: str) -> str:
        return f"https://t.me/{quote(bot_username)}?start={quote(token)}"
