from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from receipt_bot.cleanup import CleanupConflict, ReceiptCleanupService
from receipt_bot.domain.text_evidence import is_text_receipt_candidate
from receipt_bot.domain.time import to_tehran
from receipt_bot.persistence.database import Database
from receipt_bot.persistence.models import TelegramGroup, TelegramUser
from receipt_bot.persistence.repositories import (
    GroupRepository,
    MembershipRepository,
    MessageRepository,
    ReceiptJobRepository,
    TelegramUpdateRepository,
    TextReceiptJobRepository,
    UserRepository,
)
from receipt_bot.storage import ManagedStorage
from receipt_bot.telegram import texts
from receipt_bot.telegram.api import TelegramAPIError
from receipt_bot.telegram.contracts import TelegramGateway
from receipt_bot.telegram.financial import (
    FinancialInputError,
    FinancialWorkflowService,
    Period,
    parse_money_and_reason,
    parse_reason,
    period_from_argument,
    split_telegram_text,
    toman,
)
from receipt_bot.telegram.services import (
    AccessDenied,
    AuthorizationService,
    InteractionService,
    SettingsService,
    telegram_display_name,
)

SUPPORTED_DOCUMENT_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _command(text: str | None) -> tuple[str | None, str | None]:
    if not text or not text.startswith("/"):
        return None, None
    parts = text.strip().split(maxsplit=1)
    name = parts[0].split("@", 1)[0].casefold()
    argument = parts[1].strip() if len(parts) == 2 else None
    return name, argument


def _update_kind(update: dict[str, Any]) -> str:
    for kind in ("callback_query", "message", "edited_message"):
        if kind in update:
            return kind
    return "unsupported"


def _message_from_update(update: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(update.get("message"), dict):
        return update["message"]
    if isinstance(update.get("edited_message"), dict):
        return update["edited_message"]
    callback = update.get("callback_query")
    if isinstance(callback, dict) and isinstance(callback.get("message"), dict):
        return callback["message"]
    return None


class TelegramUpdateHandler:
    def __init__(
        self,
        database: Database,
        gateway: TelegramGateway,
        *,
        bot_username: str | None = None,
        interaction_ttl_minutes: int = 15,
        storage: ManagedStorage | None = None,
    ) -> None:
        self.database = database
        self.gateway = gateway
        self.bot_username = bot_username
        self.authorization = AuthorizationService(database, gateway)
        self.settings = SettingsService(database, self.authorization)
        self.interactions = InteractionService(
            database, ttl_minutes=interaction_ttl_minutes
        )
        self.financial = FinancialWorkflowService(database)
        self.cleanup = ReceiptCleanupService(database, storage) if storage else None

    async def handle(self, update: dict[str, Any]) -> bool:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return False
        kind = _update_kind(update)
        message = _message_from_update(update)
        chat = message.get("chat", {}) if message else {}
        is_group = chat.get("type") in {"group", "supergroup"}
        group: TelegramGroup | None = None
        user: TelegramUser | None = None
        actor = self._actor(update)

        with self.database.session() as session:
            if is_group and isinstance(chat.get("id"), int):
                group, _ = GroupRepository(session).get_or_create(
                    chat["id"], str(chat.get("title") or chat["id"]), active=False
                )
            if isinstance(actor.get("id"), int):
                user, _ = UserRepository(session).get_or_create(
                    actor["id"], telegram_display_name(actor), actor.get("username")
                )
                if group is not None:
                    MembershipRepository(session, group.id).observe_member(user.id)
            repository = TelegramUpdateRepository(session)
            item, _ = repository.register(
                update_id=update_id,
                update_kind=kind,
                payload=update,
                group_id=group.id if group else None,
            )
            if not repository.claim(item):
                return False

        try:
            if kind == "callback_query":
                await self._handle_callback(update["callback_query"], group, user)
            elif kind in {"message", "edited_message"}:
                await self._handle_message(
                    update[kind],
                    update_id=update_id,
                    edited=kind == "edited_message",
                    group=group,
                    user=user,
                )
        except TelegramAPIError as exc:
            with self.database.session() as session:
                TelegramUpdateRepository(session).fail_retryable(update_id, exc.code)
            return False
        except Exception:
            with self.database.session() as session:
                TelegramUpdateRepository(session).fail_retryable(update_id, "handler_error")
            raise
        with self.database.session() as session:
            TelegramUpdateRepository(session).complete(update_id)
        return True

    @staticmethod
    def _actor(update: dict[str, Any]) -> dict[str, Any]:
        callback = update.get("callback_query")
        if isinstance(callback, dict) and isinstance(callback.get("from"), dict):
            return callback["from"]
        message = _message_from_update(update)
        if message and isinstance(message.get("from"), dict):
            return message["from"]
        return {}

    async def _handle_message(
        self,
        message: dict[str, Any],
        *,
        update_id: int,
        edited: bool,
        group: TelegramGroup | None,
        user: TelegramUser | None,
    ) -> None:
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or user is None:
            return
        text = message.get("text") or message.get("caption")
        command, argument = _command(text)
        if chat.get("type") == "private":
            await self._handle_private(chat_id, user, command, argument)
            return
        if group is None:
            return

        if command == "/start":
            try:
                active_group, changed = await self.settings.activate_group(
                    telegram_chat_id=chat_id,
                    telegram_user_id=user.telegram_user_id,
                )
            except AccessDenied:
                await self.gateway.send_message(chat_id, texts.START_ADMIN_REQUIRED)
                return
            await self._store_message(message, update_id, active_group, user, edited)
            await self.gateway.send_message(
                chat_id, texts.START_ACTIVATED if changed else texts.START_ALREADY_ACTIVE
            )
            return

        if command == "/help":
            if group.active:
                await self._store_message(message, update_id, group, user, edited)
            await self.gateway.send_message(chat_id, texts.HELP_GROUP)
            return

        if not group.active:
            if command is not None:
                await self.gateway.send_message(chat_id, texts.GROUP_INACTIVE)
            return

        stored, _ = await self._store_message(message, update_id, group, user, edited)
        if command in {"/request", "/promise"}:
            try:
                await self.authorization.require_reviewer(group, user)
            except AccessDenied:
                await self.gateway.send_message(chat_id, texts.UNAUTHORIZED)
                return
            reply = message.get("reply_to_message")
            reply_message_id = reply.get("message_id") if isinstance(reply, dict) else None
            if not isinstance(reply_message_id, int):
                await self.gateway.send_message(chat_id, texts.LEDGER_CAPTURE_REPLY_REQUIRED)
                return
            try:
                state = self.financial.capture_source(
                    group.id,
                    telegram_message_id=reply_message_id,
                    operation=(
                        "request_create" if command == "/request" else "promise_create"
                    ),
                )
            except FinancialInputError as exc:
                await self.gateway.send_message(chat_id, f"⚠️ {exc}")
                return
            await self._start_action_prompt(
                group=group,
                user=user,
                chat_id=chat_id,
                operation=str(state.pop("operation")),
                state=state,
            )
            return
        if command == "/settings":
            try:
                await self.authorization.require_admin(chat_id, user.telegram_user_id)
            except AccessDenied:
                await self.gateway.send_message(chat_id, texts.UNAUTHORIZED)
                return
            await self._send_settings(group, user, chat_id)
            return

        if command == "/cleanup":
            try:
                await self.authorization.require_admin(chat_id, user.telegram_user_id)
            except AccessDenied:
                await self.gateway.send_message(chat_id, texts.UNAUTHORIZED)
                return
            if self.cleanup is None:
                await self.gateway.send_message(chat_id, texts.CLEANUP_UNAVAILABLE)
                return
            await self._send_cleanup_preview(group, user, chat_id)
            return

        if command in {
            "/reports",
            "/summary",
            "/pending",
            "/verify",
            "/receipts",
            "/transactions",
            "/balance",
        }:
            try:
                await self.authorization.require_reviewer(group, user)
            except AccessDenied:
                await self.gateway.send_message(chat_id, texts.UNAUTHORIZED)
                return
            await self._handle_financial_command(
                command,
                argument,
                group=group,
                user=user,
                chat_id=chat_id,
            )
            return

        if isinstance(message.get("users_shared"), dict):
            await self._handle_users_shared(message["users_shared"], group, user, chat_id)
            return


        reply = message.get("reply_to_message")
        prompt_message_id = reply.get("message_id") if isinstance(reply, dict) else None
        if command is None and isinstance(text, str):
            handled = await self._handle_financial_input(
                group=group,
                user=user,
                chat_id=chat_id,
                prompt_message_id=(
                    prompt_message_id if isinstance(prompt_message_id, int) else None
                ),
                text=text,
            )
            if handled:
                return

        media = self._receipt_media(message)
        if media is None:
            if isinstance(message.get("document"), dict):
                await self.gateway.send_message(chat_id, texts.UNSUPPORTED_DOCUMENT)
            elif command is None and is_text_receipt_candidate(text):
                with self.database.session() as session:
                    job, _ = TextReceiptJobRepository(session, group.id).enqueue(
                        group_id=group.id,
                        source_message_id=stored.id,
                        text=text,
                    )
                await self.gateway.send_message(
                    chat_id,
                    texts.TEXT_RECEIPT_DUPLICATE
                    if job.status == "duplicate_exact"
                    else texts.TEXT_RECEIPT_QUEUED,
                )
            return
        with self.database.session() as session:
            ReceiptJobRepository(session, group.id).enqueue(
                group_id=group.id,
                source_message_id=stored.id,
                telegram_file_id=media["file_id"],
                telegram_file_unique_id=media["file_unique_id"],
                original_filename=media.get("file_name"),
                declared_mime_type=media.get("mime_type"),
                declared_byte_size=media.get("file_size"),
            )
        await self.gateway.send_message(chat_id, texts.RECEIPT_QUEUED)

    async def _handle_private(
        self,
        chat_id: int,
        user: TelegramUser,
        command: str | None,
        argument: str | None,
    ) -> None:
        if command == "/help":
            await self.gateway.send_message(chat_id, texts.HELP_PRIVATE)
            return
        if command != "/start" or not argument:
            await self.gateway.send_message(chat_id, texts.START_PRIVATE)
            return
        interaction = self.interactions.resolve(argument, user_id=user.id)
        if interaction is None or interaction.workflow not in {
            "private_handoff",
            "private_financial",
        }:
            await self.gateway.send_message(chat_id, texts.PRIVATE_HANDOFF_INVALID)
            return
        with self.database.session() as session:
            group = session.get(TelegramGroup, interaction.group_id)
        if group is None:
            await self.gateway.send_message(chat_id, texts.PRIVATE_HANDOFF_INVALID)
            return
        try:
            await self.authorization.require_reviewer(group, user)
        except AccessDenied:
            await self.gateway.send_message(chat_id, texts.UNAUTHORIZED)
            return
        if not self.interactions.consume(argument, user_id=user.id):
            await self.gateway.send_message(chat_id, texts.PRIVATE_HANDOFF_INVALID)
            return
        if interaction.workflow == "private_handoff":
            await self.gateway.send_message(chat_id, texts.PRIVATE_HANDOFF_OK)
            return
        try:
            kind = interaction.state.get("kind")
            if kind == "payment":
                output = self.financial.payment_detail(
                    group.id,
                    int(interaction.state["payment_id"]),
                    private=True,
                )
            elif kind == "report_sources":
                output = self.financial.private_report_sources(
                    group.id,
                    Period.from_state(interaction.state),
                )
            else:
                raise FinancialInputError("invalid private detail")
        except (FinancialInputError, LookupError, KeyError, TypeError, ValueError):
            await self.gateway.send_message(chat_id, texts.PRIVATE_HANDOFF_INVALID)
            return
        await self._send_long(chat_id, output)

    async def _store_message(
        self,
        message: dict[str, Any],
        update_id: int,
        group: TelegramGroup,
        user: TelegramUser,
        edited: bool,
    ):
        occurred_at = datetime.fromtimestamp(int(message.get("date", 0)), tz=UTC)
        edit_date = message.get("edit_date")
        edited_at = (
            datetime.fromtimestamp(int(edit_date), tz=UTC)
            if edited and isinstance(edit_date, int)
            else None
        )
        reply = message.get("reply_to_message")
        with self.database.session() as session:
            return MessageRepository(session).add_idempotent(
                group_id=group.id,
                telegram_message_id=int(message["message_id"]),
                telegram_update_id=update_id,
                occurred_at=occurred_at,
                message_type=self._message_type(message),
                sender_user_id=user.id,
                text=message.get("text") or message.get("caption"),
                telegram_thread_id=message.get("message_thread_id"),
                reply_to_message_id=(
                    reply.get("message_id") if isinstance(reply, dict) else None
                ),
                edited_at=edited_at,
            )

    @staticmethod
    def _message_type(message: dict[str, Any]) -> str:
        if message.get("photo"):
            return "photo"
        if message.get("document"):
            return "document"
        if message.get("users_shared"):
            return "users_shared"
        command, _ = _command(message.get("text"))
        return "command" if command else "text"

    @staticmethod
    def _receipt_media(message: dict[str, Any]) -> dict[str, Any] | None:
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            candidates = [item for item in photos if isinstance(item, dict)]
            if candidates:
                selected = max(
                    candidates,
                    key=lambda item: (item.get("file_size") or 0, item.get("width", 0) * item.get("height", 0)),
                )
                return {**selected, "mime_type": "image/jpeg", "file_name": None}
        document = message.get("document")
        if isinstance(document, dict) and document.get("mime_type") in SUPPORTED_DOCUMENT_MIME_TYPES:
            return document
        return None

    async def _handle_financial_command(
        self,
        command: str,
        argument: str | None,
        *,
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
    ) -> None:
        if command in {"/reports", "/summary", "/receipts", "/transactions"}:
            try:
                period = period_from_argument(argument)
            except FinancialInputError as exc:
                await self.gateway.send_message(chat_id, f"⚠️ {exc}")
                return
            view = command.removeprefix("/")
            interaction = self.interactions.create(
                group_id=group.id,
                user_id=user.id,
                workflow="finance_period",
                state={"view": view, "page": 0, **period.to_state()},
            )
            await self._send_period_view(group, user, chat_id, interaction.token)
            return
        if command in {"/pending", "/verify"}:
            interaction = self.interactions.create(
                group_id=group.id,
                user_id=user.id,
                workflow="finance_browser",
                state={"view": "pending", "page": 0},
            )
            await self._send_pending_view(group, user, chat_id, interaction.token)
            return
        if command == "/balance":
            interaction = self.interactions.create(
                group_id=group.id,
                user_id=user.id,
                workflow="finance_browser",
                state={"view": "balance", "page": 0},
            )
            await self._send_balance_picker(group, user, chat_id, interaction.token)

    async def _send_period_view(
        self,
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
        token: str,
    ) -> None:
        interaction = self.interactions.resolve(token, user_id=user.id, group_id=group.id)
        if interaction is None:
            await self.gateway.send_message(chat_id, texts.CALLBACK_EXPIRED)
            return
        state = interaction.state
        period = Period.from_state(state)
        view = str(state.get("view"))
        page = max(int(state.get("page", 0)), 0)
        callback = self.interactions.callback_data
        extra_rows: list[list[dict[str, str]]] = []
        if view == "reports":
            output = self.financial.report(group.id, user.id, period)
            if self.bot_username:
                handoff = self.interactions.create(
                    group_id=group.id,
                    user_id=user.id,
                    workflow="private_financial",
                    state={"kind": "report_sources", **period.to_state()},
                )
                extra_rows.append(
                    [{"text": "منابع گزارش در خصوصی", "url": self.interactions.private_url(self.bot_username, handoff.token)}]
                )
        elif view == "summary":
            output = self.financial.summary(group.id, period)
        elif view == "receipts":
            output, payment_ids, previous, following = self.financial.receipts(
                group.id, period, page
            )
            for payment_id in payment_ids:
                row: list[dict[str, str]] = [
                    {
                        "text": f"بررسی رسید #{payment_id}",
                        "callback_data": callback(token, f"pay_{payment_id}"),
                    }
                ]
                if self.bot_username:
                    handoff = self.interactions.create(
                        group_id=group.id,
                        user_id=user.id,
                        workflow="private_financial",
                        state={"kind": "payment", "payment_id": payment_id},
                    )
                    row.append(
                        {
                            "text": "جزئیات خصوصی",
                            "url": self.interactions.private_url(self.bot_username, handoff.token),
                        }
                    )
                extra_rows.append(row)
            extra_rows.extend(self._pagination_rows(token, callback, previous, following))
        elif view == "transactions":
            output, payment_ids, previous, following = self.financial.transactions(
                group.id, period, page
            )
            for payment_id in payment_ids:
                row: list[dict[str, str]] = [
                    {
                        "text": f"بررسی رسید #{payment_id}",
                        "callback_data": callback(token, f"pay_{payment_id}"),
                    }
                ]
                if self.bot_username:
                    handoff = self.interactions.create(
                        group_id=group.id,
                        user_id=user.id,
                        workflow="private_financial",
                        state={"kind": "payment", "payment_id": payment_id},
                    )
                    row.append(
                        {
                            "text": "جزئیات خصوصی",
                            "url": self.interactions.private_url(self.bot_username, handoff.token),
                        }
                    )
                extra_rows.append(row)
            extra_rows.extend(self._pagination_rows(token, callback, previous, following))
        else:
            await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
            return
        keyboard = [
            [
                {"text": "روز قبل", "callback_data": callback(token, "period_prev")},
                {"text": "امروز", "callback_data": callback(token, "period_today")},
                {"text": "روز بعد", "callback_data": callback(token, "period_next")},
            ],
            [
                {"text": "۷ روز", "callback_data": callback(token, "period_7")},
                {"text": "۳۰ روز", "callback_data": callback(token, "period_30")},
            ],
            *extra_rows,
            [{"text": "لغو", "callback_data": callback(token, "cancel")}],
        ]
        await self._send_long(
            chat_id,
            output,
            reply_markup={"inline_keyboard": keyboard},
        )

    async def _send_pending_view(
        self,
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
        token: str,
    ) -> None:
        interaction = self.interactions.resolve(token, user_id=user.id, group_id=group.id)
        if interaction is None:
            await self.gateway.send_message(chat_id, texts.CALLBACK_EXPIRED)
            return
        page = max(int(interaction.state.get("page", 0)), 0)
        output, payment_ids, allocation_ids, previous, following = self.financial.pending(
            group.id, page
        )
        callback = self.interactions.callback_data
        rows: list[list[dict[str, str]]] = []
        for payment_id in payment_ids:
            rows.append(
                [{"text": f"رسید #{payment_id}", "callback_data": callback(token, f"pay_{payment_id}")}]
            )
        for allocation_id in allocation_ids:
            rows.append(
                [{"text": f"تخصیص #{allocation_id}", "callback_data": callback(token, f"alloc_{allocation_id}")}]
            )
        rows.extend(self._pagination_rows(token, callback, previous, following))
        rows.append([{"text": "لغو", "callback_data": callback(token, "cancel")}])
        await self.gateway.send_message(
            chat_id,
            output,
            reply_markup={"inline_keyboard": rows},
        )

    async def _send_balance_picker(
        self,
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
        token: str,
    ) -> None:
        interaction = self.interactions.resolve(token, user_id=user.id, group_id=group.id)
        if interaction is None:
            await self.gateway.send_message(chat_id, texts.CALLBACK_EXPIRED)
            return
        page = max(int(interaction.state.get("page", 0)), 0)
        output, ids, previous, following = self.financial.counterparties(group.id, page)
        callback = self.interactions.callback_data
        rows = [
            [{"text": f"شخص #{item_id}", "callback_data": callback(token, f"party_{item_id}")}]
            for item_id in ids
        ]
        rows.extend(self._pagination_rows(token, callback, previous, following))
        rows.append([{"text": "لغو", "callback_data": callback(token, "cancel")}])
        await self.gateway.send_message(
            chat_id,
            output,
            reply_markup={"inline_keyboard": rows},
        )

    @staticmethod
    def _pagination_rows(token, callback, previous: bool, following: bool):
        row: list[dict[str, str]] = []
        if previous:
            row.append({"text": "صفحه قبل", "callback_data": callback(token, "page_prev")})
        if following:
            row.append({"text": "صفحه بعد", "callback_data": callback(token, "page_next")})
        return [row] if row else []

    async def _send_payment_review(
        self,
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
        token: str,
        payment_id: int,
    ) -> None:
        try:
            output = self.financial.payment_detail(group.id, payment_id)
        except LookupError:
            await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
            return
        callback = self.interactions.callback_data
        rows: list[list[dict[str, str]]] = [
            [
                {"text": "تأیید سند", "callback_data": callback(token, f"confirm_{payment_id}")},
                {"text": "رد", "callback_data": callback(token, f"reject_{payment_id}")},
            ],
            [
                {"text": "اصلاح مبلغ", "callback_data": callback(token, f"correct_{payment_id}")},
                {"text": "اتصال/تقسیم", "callback_data": callback(token, f"link_{payment_id}")},
            ],
            [
                {"text": "تکراری", "callback_data": callback(token, f"duplicate_{payment_id}")},
                {"text": "ثبت وصول مستقل", "callback_data": callback(token, f"settlement_{payment_id}")},
            ],
        ]
        if self.bot_username:
            handoff = self.interactions.create(
                group_id=group.id,
                user_id=user.id,
                workflow="private_financial",
                state={"kind": "payment", "payment_id": payment_id},
            )
            rows.append(
                [{"text": "جزئیات خصوصی", "url": self.interactions.private_url(self.bot_username, handoff.token)}]
            )
        rows.append([{"text": "لغو", "callback_data": callback(token, "cancel")}])
        await self.gateway.send_message(
            chat_id,
            output,
            reply_markup={"inline_keyboard": rows},
        )

    async def _start_action_prompt(
        self,
        *,
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
        operation: str,
        state: dict[str, Any],
    ) -> None:
        self.interactions.consume_pending_financial_inputs(
            group_id=group.id,
            user_id=user.id,
        )
        interaction = self.interactions.create(
            group_id=group.id,
            user_id=user.id,
            workflow="finance_action",
            state={"phase": "input", "operation": operation, **state},
        )
        needs_money = operation in {
            "payment_correct",
            "payment_allocate",
            "request_create",
            "promise_create",
        }
        prompt = (
            "مبلغ، واحد و دلیل را پاسخ دهید؛ نمونه:\n"
            "۶۰۰٬۰۰۰ تومان | اصلاح طبق بررسی حسابدار\n"
            "می‌توانید همین پیام را ریپلای کنید یا پاسخ را مستقیم به‌عنوان پیام بعدی بفرستید."
            if needs_money
            else (
                "دلیل این تصمیم را بنویسید. می‌توانید همین پیام را ریپلای کنید یا دلیل را "
                "مستقیم به‌عنوان پیام بعدی بفرستید. اطلاعات کارت یا حساب را وارد نکنید."
            )
        )
        sent = await self.gateway.send_message(
            chat_id,
            prompt,
            reply_markup={"force_reply": True, "selective": True},
        )
        next_state = dict(interaction.state)
        next_state["prompt_message_id"] = int(sent["message_id"])
        self.interactions.update_state(
            interaction.token,
            user_id=user.id,
            group_id=group.id,
            state=next_state,
        )

    async def _handle_financial_input(
        self,
        *,
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
        prompt_message_id: int | None,
        text: str,
    ) -> bool:
        interaction = (
            self.interactions.find_prompt(
                group_id=group.id,
                user_id=user.id,
                prompt_message_id=prompt_message_id,
            )
            if prompt_message_id is not None
            else self.interactions.find_pending_financial_input(
                group_id=group.id,
                user_id=user.id,
            )
        )
        if interaction is None:
            return False
        try:
            await self.authorization.require_reviewer(group, user)
        except AccessDenied:
            await self.gateway.send_message(chat_id, texts.UNAUTHORIZED)
            return True
        state = dict(interaction.state)
        try:
            if state.get("operation") in {
                "payment_correct",
                "payment_allocate",
                "request_create",
                "promise_create",
            }:
                money, reason = parse_money_and_reason(text)
                state.update(amount_value=money.value, amount_unit=money.unit.value, reason=reason)
            else:
                state["reason"] = parse_reason(text)
        except FinancialInputError as exc:
            await self.gateway.send_message(chat_id, f"⚠️ {exc} دوباره به پیام درخواست پاسخ دهید.")
            return True
        state["phase"] = "ready"
        if not self.interactions.update_state(
            interaction.token,
            user_id=user.id,
            group_id=group.id,
            state=state,
        ):
            await self.gateway.send_message(chat_id, texts.CALLBACK_EXPIRED)
            return True
        callback = self.interactions.callback_data
        await self.gateway.send_message(
            chat_id,
            self.financial.action_summary(state),
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "تأیید نهایی", "callback_data": callback(interaction.token, "apply")},
                        {"text": "لغو", "callback_data": callback(interaction.token, "cancel")},
                    ]
                ]
            },
        )
        return True

    async def _send_long(
        self,
        chat_id: int,
        output: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = split_telegram_text(output)
        for index, chunk in enumerate(chunks):
            await self.gateway.send_message(
                chat_id,
                chunk,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )

    async def _send_cleanup_preview(
        self,
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
    ) -> None:
        if self.cleanup is None:
            await self.gateway.send_message(chat_id, texts.CLEANUP_UNAVAILABLE)
            return
        preview = self.cleanup.preview(group.id)
        cutoff = to_tehran(preview.cutoff).strftime("%Y/%m/%d %H:%M")
        output = texts.cleanup_preview_text(
            count=preview.file_count,
            total_bytes=preview.total_bytes,
            retention_days=preview.retention_days,
            cutoff=cutoff,
        )
        if preview.file_count == 0:
            await self.gateway.send_message(chat_id, output)
            return
        interaction = self.interactions.create(
            group_id=group.id,
            user_id=user.id,
            workflow="cleanup_preview",
            state=preview.to_state(),
        )
        callback = self.interactions.callback_data
        await self.gateway.send_message(
            chat_id,
            output,
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "ادامه و بررسی نهایی",
                            "callback_data": callback(interaction.token, "prepare"),
                        },
                        {
                            "text": "لغو",
                            "callback_data": callback(interaction.token, "cancel"),
                        },
                    ]
                ]
            },
        )

    async def _send_settings(
        self, group: TelegramGroup, user: TelegramUser, chat_id: int
    ) -> None:
        interaction = self.interactions.create(
            group_id=group.id,
            user_id=user.id,
            workflow="settings",
        )
        callback = self.interactions.callback_data
        keyboard: list[list[dict[str, str]]] = [
            [
                {"text": "۹۰ روز", "callback_data": callback(interaction.token, "retention_90")},
                {"text": "۱۸۳ روز", "callback_data": callback(interaction.token, "retention_183")},
                {"text": "۳۶۵ روز", "callback_data": callback(interaction.token, "retention_365")},
            ],
            [
                {"text": "افزودن حسابدار", "callback_data": callback(interaction.token, "reviewer_add")},
                {"text": "حذف حسابدار", "callback_data": callback(interaction.token, "reviewer_remove")},
            ],
            [
                {"text": "بازگشت", "callback_data": callback(interaction.token, "back")},
                {"text": "لغو", "callback_data": callback(interaction.token, "cancel")},
            ],
        ]
        if self.bot_username:
            handoff = self.interactions.create(
                group_id=group.id,
                user_id=user.id,
                workflow="private_handoff",
            )
            keyboard.append(
                [
                    {
                        "text": "ادامه در گفت‌وگوی خصوصی",
                        "url": self.interactions.private_url(self.bot_username, handoff.token),
                    }
                ]
            )
        await self.gateway.send_message(
            chat_id,
            texts.settings_text(title=group.title, retention_days=group.retention_days),
            reply_markup={"inline_keyboard": keyboard},
        )

    async def _handle_callback(
        self,
        callback: dict[str, Any],
        group: TelegramGroup | None,
        user: TelegramUser | None,
    ) -> None:
        callback_id = callback.get("id")
        if isinstance(callback_id, str):
            await self.gateway.answer_callback_query(callback_id)
        message = callback.get("message", {})
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        chat_id = chat.get("id")
        if group is None or user is None or not isinstance(chat_id, int) or not group.active:
            return
        data = callback.get("data")
        if not isinstance(data, str) or not data.startswith("i:"):
            await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
            return
        parts = data.split(":", 2)
        if len(parts) != 3:
            await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
            return
        _, token, action = parts
        interaction = self.interactions.resolve(token, user_id=user.id, group_id=group.id)
        if interaction is None:
            await self.gateway.send_message(chat_id, texts.CALLBACK_EXPIRED)
            return
        if interaction.workflow == "settings":
            await self._handle_settings_callback(
                interaction,
                action,
                group=group,
                user=user,
                chat_id=chat_id,
            )
            return
        if interaction.workflow in {"cleanup_preview", "cleanup_confirm"}:
            await self._handle_cleanup_callback(
                interaction,
                action,
                group=group,
                user=user,
                chat_id=chat_id,
            )
            return
        if interaction.workflow not in {
            "finance_period",
            "finance_browser",
            "finance_action",
        }:
            await self.gateway.send_message(chat_id, texts.CALLBACK_EXPIRED)
            return
        try:
            await self.authorization.require_reviewer(group, user)
        except AccessDenied:
            await self.gateway.send_message(chat_id, texts.UNAUTHORIZED)
            return
        await self._handle_financial_callback(
            interaction,
            action,
            group=group,
            user=user,
            chat_id=chat_id,
        )

    async def _handle_cleanup_callback(
        self,
        interaction,
        action: str,
        *,
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
    ) -> None:
        token = interaction.token
        try:
            await self.authorization.require_admin(chat_id, user.telegram_user_id)
        except AccessDenied:
            await self.gateway.send_message(chat_id, texts.UNAUTHORIZED)
            return
        if self.cleanup is None:
            await self.gateway.send_message(chat_id, texts.CLEANUP_UNAVAILABLE)
            return
        if action == "cancel":
            self.interactions.consume(token, user_id=user.id, group_id=group.id)
            await self.gateway.send_message(chat_id, texts.CALLBACK_CANCELLED)
            return
        if interaction.workflow == "cleanup_preview" and action == "prepare":
            try:
                preview = self.cleanup.refresh(interaction.state)
            except CleanupConflict:
                self.interactions.consume(token, user_id=user.id, group_id=group.id)
                await self.gateway.send_message(chat_id, texts.CLEANUP_CHANGED)
                return
            self.interactions.consume(token, user_id=user.id, group_id=group.id)
            confirmation = self.interactions.create(
                group_id=group.id,
                user_id=user.id,
                workflow="cleanup_confirm",
                state=preview.to_state(),
            )
            callback = self.interactions.callback_data
            await self.gateway.send_message(
                chat_id,
                texts.cleanup_final_text(
                    count=preview.file_count,
                    total_bytes=preview.total_bytes,
                ),
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "حذف قطعی تصاویر",
                                "callback_data": callback(confirmation.token, "delete"),
                            },
                            {
                                "text": "لغو",
                                "callback_data": callback(confirmation.token, "cancel"),
                            },
                        ]
                    ]
                },
            )
            return
        if interaction.workflow == "cleanup_confirm" and action == "delete":
            try:
                preview = self.cleanup.refresh(interaction.state)
                result = self.cleanup.execute(preview, actor_user_id=user.id)
            except CleanupConflict:
                self.interactions.consume(token, user_id=user.id, group_id=group.id)
                await self.gateway.send_message(chat_id, texts.CLEANUP_CHANGED)
                return
            self.interactions.consume(token, user_id=user.id, group_id=group.id)
            await self.gateway.send_message(
                chat_id,
                texts.cleanup_result_text(
                    deleted=result.deleted_count,
                    deleted_bytes=result.deleted_bytes,
                    missing=result.missing_count,
                    failed=result.failed_count,
                ),
            )
            return
        await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)

    async def _handle_settings_callback(
        self,
        interaction,
        action: str,
        *,
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
    ) -> None:
        token = interaction.token
        try:
            await self.authorization.require_admin(chat_id, user.telegram_user_id)
        except AccessDenied:
            await self.gateway.send_message(chat_id, texts.UNAUTHORIZED)
            return

        if action == "cancel":
            self.interactions.consume(token, user_id=user.id, group_id=group.id)
            await self.gateway.send_message(chat_id, texts.CALLBACK_CANCELLED)
            return
        if action == "back":
            self.interactions.consume(token, user_id=user.id, group_id=group.id)
            await self._send_settings(group, user, chat_id)
            return
        if action.startswith("retention_"):
            try:
                days = int(action.removeprefix("retention_"))
                await self.settings.set_retention_days(
                    telegram_chat_id=chat_id,
                    telegram_user_id=user.telegram_user_id,
                    days=days,
                )
            except ValueError:
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            self.interactions.consume(token, user_id=user.id, group_id=group.id)
            await self.gateway.send_message(chat_id, texts.retention_changed(days))
            return
        if action in {"reviewer_add", "reviewer_remove"}:
            self.interactions.consume(token, user_id=user.id, group_id=group.id)
            request_id = secrets.randbelow(2_000_000_000) + 1
            reviewer_action = "grant" if action == "reviewer_add" else "revoke"
            self.interactions.create(
                group_id=group.id,
                user_id=user.id,
                workflow="settings_reviewer",
                state={"request_id": request_id, "reviewer_action": reviewer_action},
            )
            keyboard = {
                "keyboard": [
                    [
                        {
                            "text": "انتخاب کاربر",
                            "request_users": {
                                "request_id": request_id,
                                "user_is_bot": False,
                                "max_quantity": 1,
                                "request_name": True,
                                "request_username": True,
                            },
                        }
                    ]
                ],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            }
            await self.gateway.send_message(
                chat_id, texts.reviewer_prompt(reviewer_action), reply_markup=keyboard
            )
            return
        await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)

    async def _handle_financial_callback(
        self,
        interaction,
        action: str,
        *,
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
    ) -> None:
        token = interaction.token
        if action == "cancel":
            self.interactions.consume(token, user_id=user.id, group_id=group.id)
            await self.gateway.send_message(chat_id, texts.CALLBACK_CANCELLED)
            return
        if interaction.workflow == "finance_action":
            if action != "apply":
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            try:
                result = self.financial.apply_action(
                    token,
                    user_id=user.id,
                    group_id=group.id,
                )
            except (FinancialInputError, LookupError, KeyError, TypeError, ValueError) as exc:
                await self.gateway.send_message(chat_id, f"⚠️ عملیات انجام نشد: {exc}")
                return
            await self.gateway.send_message(chat_id, result)
            return

        state = dict(interaction.state)
        if action in {"page_prev", "page_next"}:
            page = max(int(state.get("page", 0)) + (-1 if action == "page_prev" else 1), 0)
            state["page"] = page
            self.interactions.update_state(
                token, user_id=user.id, group_id=group.id, state=state
            )
            if interaction.workflow == "finance_period":
                await self._send_period_view(group, user, chat_id, token)
            elif state.get("view") == "pending":
                await self._send_pending_view(group, user, chat_id, token)
            else:
                await self._send_balance_picker(group, user, chat_id, token)
            return

        if interaction.workflow == "finance_period" and action.startswith("period_"):
            period = Period.from_state(state)
            if action == "period_prev":
                period = period.shifted(-1)
            elif action == "period_next":
                period = period.shifted(1)
            elif action == "period_today":
                period = Period.today()
            elif action in {"period_7", "period_30"}:
                days = int(action.removeprefix("period_"))
                period = Period(Period.today().start_date - timedelta(days=days - 1), days)
            else:
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            state.update(period.to_state())
            state["page"] = 0
            self.interactions.update_state(
                token, user_id=user.id, group_id=group.id, state=state
            )
            await self._send_period_view(group, user, chat_id, token)
            return

        if action.startswith("party_"):
            try:
                output = self.financial.balance(group.id, int(action.removeprefix("party_")))
            except (LookupError, ValueError):
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            await self.gateway.send_message(chat_id, output)
            return

        if action.startswith("pay_"):
            try:
                payment_id = int(action.removeprefix("pay_"))
            except ValueError:
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            await self._send_payment_review(group, user, chat_id, token, payment_id)
            return

        if action.startswith("alloc_"):
            try:
                allocation_id = int(action.removeprefix("alloc_"))
                output = self.financial.allocation_detail(group.id, allocation_id)
            except (LookupError, ValueError):
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            callback = self.interactions.callback_data
            await self.gateway.send_message(
                chat_id,
                output,
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "تأیید تخصیص", "callback_data": callback(token, f"aconfirm_{allocation_id}")},
                            {"text": "لغو تخصیص", "callback_data": callback(token, f"acancel_{allocation_id}")},
                        ]
                    ]
                },
            )
            return

        for prefix, operation in (
            ("confirm_", "payment_confirm"),
            ("reject_", "payment_reject"),
            ("correct_", "payment_correct"),
            ("settlement_", "payment_settlement"),
        ):
            if action.startswith(prefix):
                try:
                    payment_id = int(action.removeprefix(prefix))
                    self.financial.payment_detail(group.id, payment_id)
                except (LookupError, ValueError):
                    await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                    return
                await self._start_action_prompt(
                    group=group,
                    user=user,
                    chat_id=chat_id,
                    operation=operation,
                    state={"payment_id": payment_id},
                )
                return

        for prefix, operation in (
            ("aconfirm_", "allocation_confirm"),
            ("acancel_", "allocation_cancel"),
        ):
            if action.startswith(prefix):
                try:
                    allocation_id = int(action.removeprefix(prefix))
                    self.financial.allocation_detail(group.id, allocation_id)
                except (LookupError, ValueError):
                    await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                    return
                await self._start_action_prompt(
                    group=group,
                    user=user,
                    chat_id=chat_id,
                    operation=operation,
                    state={"allocation_id": allocation_id},
                )
                return

        if action.startswith("duplicate_"):
            try:
                payment_id = int(action.removeprefix("duplicate_"))
            except ValueError:
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            candidates = self.financial.canonical_payments(group.id, payment_id)
            callback = self.interactions.callback_data
            rows = [
                [
                    {
                        "text": f"رسید اصلی #{item_id} — {toman(amount)}",
                        "callback_data": callback(token, f"dupc_{payment_id}_{item_id}"),
                    }
                ]
                for item_id, amount in candidates
            ]
            if not rows:
                await self.gateway.send_message(chat_id, "رسید اصلی قابل انتخابی وجود ندارد.")
                return
            await self.gateway.send_message(
                chat_id,
                "رسید اصلی را انتخاب کنید. انتخاب نهایی پس از ثبت دلیل انجام می‌شود.",
                reply_markup={"inline_keyboard": rows},
            )
            return

        if action.startswith("dupc_"):
            try:
                _, payment_id, canonical_id = action.split("_", 2)
                payment_id, canonical_id = int(payment_id), int(canonical_id)
            except ValueError:
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            canonical_ids = {item[0] for item in self.financial.canonical_payments(group.id, payment_id)}
            if canonical_id not in canonical_ids:
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            await self._start_action_prompt(
                group=group,
                user=user,
                chat_id=chat_id,
                operation="payment_duplicate",
                state={"payment_id": payment_id, "canonical_payment_id": canonical_id},
            )
            return

        if action.startswith("link_"):
            try:
                payment_id = int(action.removeprefix("link_"))
            except ValueError:
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            candidates = self.financial.open_requests(group.id, payment_id)
            callback = self.interactions.callback_data
            rows = [
                [
                    {
                        "text": f"درخواست #{request_id} — {name} — تا {toman(maximum)}",
                        "callback_data": callback(token, f"reqc_{payment_id}_{request_id}"),
                    }
                ]
                for request_id, name, maximum in candidates
            ]
            if not rows:
                await self.gateway.send_message(chat_id, "درخواست سازگار و بازی برای اتصال وجود ندارد.")
                return
            await self.gateway.send_message(
                chat_id,
                "درخواست را انتخاب کنید. مبلغ تخصیص در مرحله بعد دریافت می‌شود.",
                reply_markup={"inline_keyboard": rows},
            )
            return

        if action.startswith("reqc_"):
            try:
                _, payment_id, request_id = action.split("_", 2)
                payment_id, request_id = int(payment_id), int(request_id)
            except ValueError:
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            candidate_ids = {item[0] for item in self.financial.open_requests(group.id, payment_id)}
            if request_id not in candidate_ids:
                await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)
                return
            await self._start_action_prompt(
                group=group,
                user=user,
                chat_id=chat_id,
                operation="payment_allocate",
                state={"payment_id": payment_id, "request_id": request_id},
            )
            return

        await self.gateway.send_message(chat_id, texts.CALLBACK_UNKNOWN)

    async def _handle_users_shared(
        self,
        shared: dict[str, Any],
        group: TelegramGroup,
        user: TelegramUser,
        chat_id: int,
    ) -> None:
        request_id = shared.get("request_id")
        shared_users = shared.get("users")
        if not isinstance(request_id, int) or not isinstance(shared_users, list) or not shared_users:
            return
        interaction = self.interactions.find_request(
            group_id=group.id, user_id=user.id, request_id=request_id
        )
        if interaction is None:
            return
        profile = shared_users[0]
        target_id = profile.get("user_id") if isinstance(profile, dict) else None
        if not isinstance(target_id, int):
            return
        active = interaction.state.get("reviewer_action") == "grant"
        try:
            target = await self.settings.set_accountant(
                telegram_chat_id=chat_id,
                actor_telegram_user_id=user.telegram_user_id,
                target_telegram_user_id=target_id,
                active=active,
                target_profile={
                    "id": target_id,
                    "first_name": profile.get("first_name"),
                    "last_name": profile.get("last_name"),
                    "username": profile.get("username"),
                },
            )
        except AccessDenied:
            await self.gateway.send_message(chat_id, texts.UNAUTHORIZED)
            return
        self.interactions.consume(
            interaction.token, user_id=user.id, group_id=group.id
        )
        await self.gateway.send_message(
            chat_id,
            texts.reviewer_changed(target.display_name, active),
            reply_markup={"remove_keyboard": True},
        )
