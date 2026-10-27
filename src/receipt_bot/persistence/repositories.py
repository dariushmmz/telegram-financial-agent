from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from receipt_bot.persistence.models import (
    AuditEvent,
    Counterparty,
    ExtractionAttempt,
    GroupMembership,
    InteractionSession,
    Payment,
    PaymentEvidence,
    PaymentRequest,
    ReceiptImage,
    ReceiptJob,
    RecordRevision,
    RequestPaymentAllocation,
    TelegramGroup,
    TelegramMessage,
    TextReceiptJob,
    TelegramUpdate,
    TelegramUser,
)
from receipt_bot.domain.enums import EvidenceKind, MembershipRole, ReceiptJobState, TelegramUpdateState
from receipt_bot.domain.time import utc_now
from receipt_bot.extraction.prompt import PROMPT_VERSION
from receipt_bot.extraction.schema import ExtractionOutcome
from receipt_bot.domain.text_evidence import text_evidence_sha256


class GroupRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, telegram_chat_id: int, title: str) -> TelegramGroup:
        group = TelegramGroup(telegram_chat_id=telegram_chat_id, title=title)
        self.session.add(group)
        self.session.flush()
        return group

    def get_by_chat_id(self, telegram_chat_id: int) -> TelegramGroup | None:
        return self.session.scalar(
            select(TelegramGroup).where(TelegramGroup.telegram_chat_id == telegram_chat_id)
        )

    def get_or_create(
        self, telegram_chat_id: int, title: str, *, active: bool = False
    ) -> tuple[TelegramGroup, bool]:
        group = self.get_by_chat_id(telegram_chat_id)
        if group is not None:
            if title and group.title != title:
                group.title = title
            return group, False
        group = TelegramGroup(telegram_chat_id=telegram_chat_id, title=title, active=active)
        self.session.add(group)
        self.session.flush()
        return group, True

    def set_active(self, group: TelegramGroup, active: bool) -> None:
        group.active = active
        self.session.flush()


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, telegram_user_id: int, display_name: str) -> TelegramUser:
        user = TelegramUser(telegram_user_id=telegram_user_id, display_name=display_name)
        self.session.add(user)
        self.session.flush()
        return user

    def get_by_telegram_id(self, telegram_user_id: int) -> TelegramUser | None:
        return self.session.scalar(
            select(TelegramUser).where(TelegramUser.telegram_user_id == telegram_user_id)
        )

    def get_or_create(
        self, telegram_user_id: int, display_name: str, username: str | None = None
    ) -> tuple[TelegramUser, bool]:
        user = self.get_by_telegram_id(telegram_user_id)
        if user is not None:
            user.display_name = display_name or user.display_name
            if username is not None:
                user.username = username
            return user, False
        user = TelegramUser(
            telegram_user_id=telegram_user_id,
            display_name=display_name or str(telegram_user_id),
            username=username,
        )
        self.session.add(user)
        self.session.flush()
        return user, True


class MembershipRepository:
    def __init__(self, session: Session, group_id: int) -> None:
        self.session = session
        self.group_id = group_id

    def observe_member(self, user_id: int) -> GroupMembership:
        membership = self.session.get(GroupMembership, (self.group_id, user_id))
        if membership is None:
            membership = GroupMembership(
                group_id=self.group_id,
                user_id=user_id,
                role=MembershipRole.MEMBER.value,
                active=True,
            )
            self.session.add(membership)
            self.session.flush()
        return membership

    def set_accountant(
        self, *, user_id: int, active: bool, actor_user_id: int
    ) -> GroupMembership:
        membership = self.session.get(GroupMembership, (self.group_id, user_id))
        now = utc_now()
        if membership is None:
            membership = GroupMembership(group_id=self.group_id, user_id=user_id)
            self.session.add(membership)
        membership.role = MembershipRole.ACCOUNTANT.value
        membership.active = active
        if active:
            membership.granted_by_user_id = actor_user_id
            membership.granted_at = now
            membership.revoked_by_user_id = None
            membership.revoked_at = None
        else:
            membership.revoked_by_user_id = actor_user_id
            membership.revoked_at = now
        self.session.flush()
        return membership

    def is_active_accountant(self, user_id: int) -> bool:
        membership = self.session.get(GroupMembership, (self.group_id, user_id))
        return bool(
            membership
            and membership.active
            and membership.role == MembershipRole.ACCOUNTANT.value
        )


class TelegramUpdateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def register(
        self,
        *,
        update_id: int,
        update_kind: str,
        payload: dict[str, Any],
        group_id: int | None,
    ) -> tuple[TelegramUpdate, bool]:
        item = self.session.get(TelegramUpdate, update_id)
        if item is not None:
            if item.group_id is None and group_id is not None:
                item.group_id = group_id
            return item, False
        item = TelegramUpdate(
            update_id=update_id,
            group_id=group_id,
            update_kind=update_kind,
            payload=payload,
            status=TelegramUpdateState.RECEIVED.value,
        )
        self.session.add(item)
        self.session.flush()
        return item, True

    def claim(self, item: TelegramUpdate) -> bool:
        if item.status == TelegramUpdateState.COMPLETED.value:
            return False
        item.status = TelegramUpdateState.PROCESSING.value
        item.attempt_count += 1
        item.processing_started_at = utc_now()
        item.last_error_code = None
        self.session.flush()
        return True

    def complete(self, update_id: int) -> None:
        item = self.session.get(TelegramUpdate, update_id)
        if item is None:
            raise LookupError("Telegram update does not exist")
        item.status = TelegramUpdateState.COMPLETED.value
        item.completed_at = utc_now()
        item.payload = {}
        item.last_error_code = None
        self.session.flush()

    def fail_retryable(self, update_id: int, error_code: str) -> None:
        item = self.session.get(TelegramUpdate, update_id)
        if item is None:
            raise LookupError("Telegram update does not exist")
        item.status = TelegramUpdateState.FAILED_RETRYABLE.value
        item.last_error_code = error_code[:128]
        self.session.flush()

    def pending_payloads(self) -> list[dict[str, Any]]:
        return [
            item.payload
            for item in self.session.scalars(
                select(TelegramUpdate)
                .where(TelegramUpdate.status != TelegramUpdateState.COMPLETED.value)
                .order_by(TelegramUpdate.update_id)
            )
            if item.payload
        ]

    def next_offset(self) -> int | None:
        latest = self.session.scalar(
            select(TelegramUpdate.update_id).order_by(TelegramUpdate.update_id.desc()).limit(1)
        )
        return None if latest is None else latest + 1


class MessageRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_idempotent(
        self,
        *,
        group_id: int,
        telegram_message_id: int,
        telegram_update_id: int | None,
        occurred_at: datetime,
        message_type: str,
        sender_user_id: int | None = None,
        text: str | None = None,
        telegram_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        edited_at: datetime | None = None,
    ) -> tuple[TelegramMessage, bool]:
        existing = self.session.scalar(
            select(TelegramMessage).where(
                TelegramMessage.group_id == group_id,
                TelegramMessage.telegram_message_id == telegram_message_id,
            )
        )
        if existing is not None:
            return existing, False
        message = TelegramMessage(
            group_id=group_id,
            telegram_message_id=telegram_message_id,
            telegram_update_id=telegram_update_id,
            occurred_at=occurred_at,
            message_type=message_type,
            sender_user_id=sender_user_id,
            text=text,
            telegram_thread_id=telegram_thread_id,
            reply_to_message_id=reply_to_message_id,
            edited_at=edited_at,
        )
        self.session.add(message)
        self.session.flush()
        return message, True


class ReceiptJobRepository:
    CLAIMABLE_DOWNLOAD = {
        ReceiptJobState.QUEUED_DOWNLOAD.value,
        ReceiptJobState.DOWNLOAD_FAILED_RETRYABLE.value,
    }
    CLAIMABLE_EXTRACTION = {
        ReceiptJobState.QUEUED_EXTRACTION.value,
        ReceiptJobState.EXTRACTION_FAILED_RETRYABLE.value,
    }

    def __init__(self, session: Session, group_id: int | None = None) -> None:
        self.session = session
        self.group_id = group_id

    def get(self, job_id: int) -> ReceiptJob:
        item = self.session.get(ReceiptJob, job_id)
        if item is None:
            raise LookupError("receipt job does not exist")
        if self.group_id is not None and item.group_id != self.group_id:
            raise LookupError("receipt job does not belong to this group")
        return item

    def enqueue(
        self,
        *,
        group_id: int,
        source_message_id: int,
        telegram_file_id: str,
        telegram_file_unique_id: str,
        original_filename: str | None,
        declared_mime_type: str | None,
        declared_byte_size: int | None,
    ) -> tuple[ReceiptJob, bool]:
        existing = self.session.scalar(
            select(ReceiptJob).where(ReceiptJob.source_message_id == source_message_id)
        )
        if existing is not None:
            return existing, False
        item = ReceiptJob(
            group_id=group_id,
            source_message_id=source_message_id,
            telegram_file_id=telegram_file_id,
            telegram_file_unique_id=telegram_file_unique_id,
            original_filename=original_filename,
            declared_mime_type=declared_mime_type,
            declared_byte_size=declared_byte_size,
            status=ReceiptJobState.QUEUED_DOWNLOAD.value,
        )
        self.session.add(item)
        self.session.flush()
        return item, True

    def claim_next(
        self, *, now: datetime | None = None, allow_extraction: bool = True
    ) -> ReceiptJob | None:
        now = now or utc_now()
        statuses = set(self.CLAIMABLE_DOWNLOAD)
        if allow_extraction:
            statuses.update(self.CLAIMABLE_EXTRACTION)
        conditions = [ReceiptJob.status.in_(statuses), ReceiptJob.available_at <= now]
        if self.group_id is not None:
            conditions.append(ReceiptJob.group_id == self.group_id)
        item = self.session.scalar(
            select(ReceiptJob).where(*conditions).order_by(ReceiptJob.available_at, ReceiptJob.id).limit(1)
        )
        if item is None:
            return None
        if item.status in self.CLAIMABLE_DOWNLOAD:
            item.status = ReceiptJobState.DOWNLOADING.value
        else:
            item.status = ReceiptJobState.EXTRACTING.value
        item.attempt_count += 1
        item.processing_started_at = now
        item.last_error_code = None
        self.session.flush()
        return item

    def recover_stale(self, *, before: datetime) -> int:
        items = list(
            self.session.scalars(
                select(ReceiptJob).where(
                    ReceiptJob.status.in_(
                        [ReceiptJobState.DOWNLOADING.value, ReceiptJobState.EXTRACTING.value]
                    ),
                    ReceiptJob.processing_started_at < before,
                )
            )
        )
        for item in items:
            item.status = (
                ReceiptJobState.QUEUED_DOWNLOAD.value
                if item.status == ReceiptJobState.DOWNLOADING.value
                else ReceiptJobState.QUEUED_EXTRACTION.value
            )
            item.processing_started_at = None
            item.available_at = utc_now()
        self.session.flush()
        return len(items)

    def find_by_file_unique_id(self, group_id: int, file_unique_id: str) -> ReceiptImage | None:
        return self.session.scalar(
            select(ReceiptImage)
            .where(
                ReceiptImage.group_id == group_id,
                ReceiptImage.telegram_file_unique_id == file_unique_id,
            )
            .order_by(ReceiptImage.id)
            .limit(1)
        )

    def find_exact(self, group_id: int, sha256: str) -> ReceiptImage | None:
        return self.session.scalar(
            select(ReceiptImage)
            .where(ReceiptImage.group_id == group_id, ReceiptImage.sha256 == sha256)
            .order_by(ReceiptImage.id)
            .limit(1)
        )

    def find_candidate(self, group_id: int, perceptual_hash: str) -> ReceiptImage | None:
        return self.session.scalar(
            select(ReceiptImage)
            .where(
                ReceiptImage.group_id == group_id,
                ReceiptImage.perceptual_hash == perceptual_hash,
            )
            .order_by(ReceiptImage.id)
            .limit(1)
        )

    def evidence_for_receipt(self, group_id: int, receipt_id: int) -> PaymentEvidence | None:
        return self.session.scalar(
            select(PaymentEvidence)
            .where(
                PaymentEvidence.group_id == group_id,
                PaymentEvidence.receipt_image_id == receipt_id,
            )
            .order_by(PaymentEvidence.id)
            .limit(1)
        )

    def ensure_evidence_for_receipt(self, receipt: ReceiptImage) -> PaymentEvidence:
        existing = self.evidence_for_receipt(receipt.group_id, receipt.id)
        if existing is not None:
            return existing
        evidence = PaymentEvidence(
            group_id=receipt.group_id,
            source_message_id=receipt.source_message_id,
            evidence_kind=EvidenceKind.IMAGE.value,
            receipt_image_id=receipt.id,
            content_sha256=receipt.sha256,
            duplicate_disposition=receipt.duplicate_disposition,
        )
        self.session.add(evidence)
        self.session.flush()
        return evidence

    def create_image_evidence(
        self,
        *,
        job: ReceiptJob,
        receipt: ReceiptImage,
        duplicate_of_evidence_id: int | None = None,
        duplicate_disposition: str | None = None,
    ) -> PaymentEvidence:
        existing = self.session.scalar(
            select(PaymentEvidence).where(PaymentEvidence.source_message_id == job.source_message_id)
        )
        if existing is not None:
            job.evidence_id = existing.id
            return existing
        evidence = PaymentEvidence(
            group_id=job.group_id,
            source_message_id=job.source_message_id,
            evidence_kind=EvidenceKind.IMAGE.value,
            receipt_image_id=receipt.id,
            content_sha256=receipt.sha256,
            duplicate_of_evidence_id=duplicate_of_evidence_id,
            duplicate_disposition=duplicate_disposition,
        )
        self.session.add(evidence)
        self.session.flush()
        job.evidence_id = evidence.id
        return evidence

    def create_receipt(
        self,
        *,
        job: ReceiptJob,
        mime_type: str,
        byte_size: int,
        sha256: str,
        perceptual_hash: str,
        storage_key: str | None,
        storage_state: str = "present",
        duplicate_of_receipt_id: int | None = None,
        duplicate_disposition: str | None = None,
    ) -> ReceiptImage:
        receipt = ReceiptImage(
            group_id=job.group_id,
            source_message_id=job.source_message_id,
            telegram_file_id=job.telegram_file_id,
            telegram_file_unique_id=job.telegram_file_unique_id,
            original_filename=job.original_filename,
            mime_type=mime_type,
            byte_size=byte_size,
            sha256=sha256,
            perceptual_hash=perceptual_hash,
            storage_key=storage_key,
            storage_state=storage_state,
            duplicate_of_receipt_id=duplicate_of_receipt_id,
            duplicate_disposition=duplicate_disposition,
        )
        self.session.add(receipt)
        self.session.flush()
        job.receipt_id = receipt.id
        return receipt

    def queue_extraction(self, job: ReceiptJob) -> None:
        job.status = ReceiptJobState.QUEUED_EXTRACTION.value
        job.available_at = utc_now()
        job.processing_started_at = None
        self.session.flush()

    def complete(self, job: ReceiptJob, *, duplicate: bool = False) -> None:
        job.status = (
            ReceiptJobState.DUPLICATE_EXACT.value if duplicate else ReceiptJobState.SUCCEEDED.value
        )
        job.finished_at = utc_now()
        job.processing_started_at = None
        self.session.flush()

    def fail(
        self,
        job: ReceiptJob,
        *,
        stage: str,
        error_code: str,
        retryable: bool,
        retry_delay_seconds: float = 30,
    ) -> None:
        if retryable:
            job.status = (
                ReceiptJobState.DOWNLOAD_FAILED_RETRYABLE.value
                if stage == "download"
                else ReceiptJobState.EXTRACTION_FAILED_RETRYABLE.value
            )
            job.available_at = utc_now() + timedelta(seconds=retry_delay_seconds)
        else:
            job.status = ReceiptJobState.FAILED_FINAL.value
            job.finished_at = utc_now()
        job.last_error_code = error_code[:128]
        job.processing_started_at = None
        self.session.flush()


class TextReceiptJobRepository:
    CLAIMABLE = {
        ReceiptJobState.QUEUED_EXTRACTION.value,
        ReceiptJobState.EXTRACTION_FAILED_RETRYABLE.value,
    }

    def __init__(self, session: Session, group_id: int | None = None) -> None:
        self.session = session
        self.group_id = group_id

    def get(self, job_id: int) -> TextReceiptJob:
        item = self.session.get(TextReceiptJob, job_id)
        if item is None:
            raise LookupError("text receipt job does not exist")
        if self.group_id is not None and item.group_id != self.group_id:
            raise LookupError("text receipt job does not belong to this group")
        return item

    def enqueue(
        self, *, group_id: int, source_message_id: int, text: str
    ) -> tuple[TextReceiptJob, bool]:
        existing = self.session.scalar(
            select(TextReceiptJob).where(TextReceiptJob.source_message_id == source_message_id)
        )
        if existing is not None:
            return existing, False
        digest = text_evidence_sha256(text)
        duplicate = self.session.scalar(
            select(PaymentEvidence)
            .where(
                PaymentEvidence.group_id == group_id,
                PaymentEvidence.evidence_kind == EvidenceKind.TELEGRAM_TEXT.value,
                PaymentEvidence.content_sha256 == digest,
            )
            .order_by(PaymentEvidence.id)
            .limit(1)
        )
        evidence = PaymentEvidence(
            group_id=group_id,
            source_message_id=source_message_id,
            evidence_kind=EvidenceKind.TELEGRAM_TEXT.value,
            content_sha256=digest,
            duplicate_of_evidence_id=duplicate.id if duplicate else None,
            duplicate_disposition="exact" if duplicate else None,
        )
        self.session.add(evidence)
        self.session.flush()
        item = TextReceiptJob(
            group_id=group_id,
            source_message_id=source_message_id,
            evidence_id=evidence.id,
            status=(
                ReceiptJobState.DUPLICATE_EXACT.value
                if duplicate
                else ReceiptJobState.QUEUED_EXTRACTION.value
            ),
            finished_at=utc_now() if duplicate else None,
        )
        self.session.add(item)
        self.session.flush()
        return item, True

    def claim_next(self, *, now: datetime | None = None) -> TextReceiptJob | None:
        now = now or utc_now()
        conditions = [TextReceiptJob.status.in_(self.CLAIMABLE), TextReceiptJob.available_at <= now]
        if self.group_id is not None:
            conditions.append(TextReceiptJob.group_id == self.group_id)
        item = self.session.scalar(
            select(TextReceiptJob)
            .where(*conditions)
            .order_by(TextReceiptJob.available_at, TextReceiptJob.id)
            .limit(1)
        )
        if item is None:
            return None
        item.status = ReceiptJobState.EXTRACTING.value
        item.attempt_count += 1
        item.processing_started_at = now
        item.last_error_code = None
        self.session.flush()
        return item

    def recover_stale(self, *, before: datetime) -> int:
        items = list(
            self.session.scalars(
                select(TextReceiptJob).where(
                    TextReceiptJob.status == ReceiptJobState.EXTRACTING.value,
                    TextReceiptJob.processing_started_at < before,
                )
            )
        )
        for item in items:
            item.status = ReceiptJobState.QUEUED_EXTRACTION.value
            item.processing_started_at = None
            item.available_at = utc_now()
        self.session.flush()
        return len(items)

    def complete(self, job: TextReceiptJob) -> None:
        job.status = ReceiptJobState.SUCCEEDED.value
        job.finished_at = utc_now()
        job.processing_started_at = None
        self.session.flush()

    def fail(
        self,
        job: TextReceiptJob,
        *,
        error_code: str,
        retryable: bool,
        retry_delay_seconds: float = 30,
    ) -> None:
        if retryable:
            job.status = ReceiptJobState.EXTRACTION_FAILED_RETRYABLE.value
            job.available_at = utc_now() + timedelta(seconds=retry_delay_seconds)
        else:
            job.status = ReceiptJobState.FAILED_FINAL.value
            job.finished_at = utc_now()
        job.last_error_code = error_code[:128]
        job.processing_started_at = None
        self.session.flush()


class InteractionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        group_id: int,
        initiating_user_id: int,
        workflow: str,
        state: dict[str, Any],
        expires_at: datetime,
    ) -> InteractionSession:
        item = InteractionSession(
            token=secrets.token_urlsafe(18),
            group_id=group_id,
            initiating_user_id=initiating_user_id,
            workflow=workflow,
            state=state,
            expires_at=expires_at,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def resolve(
        self,
        token: str,
        *,
        initiating_user_id: int,
        group_id: int | None = None,
        now: datetime | None = None,
    ) -> InteractionSession | None:
        now = now or utc_now()
        conditions = [
            InteractionSession.token == token,
            InteractionSession.initiating_user_id == initiating_user_id,
            InteractionSession.consumed_at.is_(None),
            InteractionSession.expires_at > now,
        ]
        if group_id is not None:
            conditions.append(InteractionSession.group_id == group_id)
        return self.session.scalar(select(InteractionSession).where(*conditions))

    def find_request(
        self,
        *,
        group_id: int,
        initiating_user_id: int,
        workflow: str,
        request_id: int,
        now: datetime | None = None,
    ) -> InteractionSession | None:
        now = now or utc_now()
        candidates = self.session.scalars(
            select(InteractionSession)
            .where(
                InteractionSession.group_id == group_id,
                InteractionSession.initiating_user_id == initiating_user_id,
                InteractionSession.workflow == workflow,
                InteractionSession.consumed_at.is_(None),
                InteractionSession.expires_at > now,
            )
            .order_by(InteractionSession.created_at.desc())
        )
        return next((item for item in candidates if item.state.get("request_id") == request_id), None)

    def find_prompt(
        self,
        *,
        group_id: int,
        initiating_user_id: int,
        prompt_message_id: int,
        now: datetime | None = None,
    ) -> InteractionSession | None:
        now = now or utc_now()
        candidates = self.session.scalars(
            select(InteractionSession)
            .where(
                InteractionSession.group_id == group_id,
                InteractionSession.initiating_user_id == initiating_user_id,
                InteractionSession.workflow == "finance_action",
                InteractionSession.consumed_at.is_(None),
                InteractionSession.expires_at > now,
            )
            .order_by(InteractionSession.created_at.desc())
        )
        return next(
            (
                item
                for item in candidates
                if item.state.get("phase") == "input"
                and item.state.get("prompt_message_id") == prompt_message_id
            ),
            None,
        )

    def find_pending_financial_input(
        self,
        *,
        group_id: int,
        initiating_user_id: int,
        now: datetime | None = None,
    ) -> InteractionSession | None:
        now = now or utc_now()
        candidates = self.session.scalars(
            select(InteractionSession)
            .where(
                InteractionSession.group_id == group_id,
                InteractionSession.initiating_user_id == initiating_user_id,
                InteractionSession.workflow == "finance_action",
                InteractionSession.consumed_at.is_(None),
                InteractionSession.expires_at > now,
            )
            .order_by(InteractionSession.created_at.desc())
        )
        return next(
            (item for item in candidates if item.state.get("phase") == "input"),
            None,
        )

    def consume_pending_financial_inputs(
        self,
        *,
        group_id: int,
        initiating_user_id: int,
        now: datetime | None = None,
    ) -> int:
        now = now or utc_now()
        candidates = self.session.scalars(
            select(InteractionSession).where(
                InteractionSession.group_id == group_id,
                InteractionSession.initiating_user_id == initiating_user_id,
                InteractionSession.workflow == "finance_action",
                InteractionSession.consumed_at.is_(None),
                InteractionSession.expires_at > now,
            )
        )
        consumed = 0
        for item in candidates:
            if item.state.get("phase") == "input":
                self.consume(item)
                consumed += 1
        return consumed

    def update_state(self, item: InteractionSession, state: dict[str, Any]) -> None:
        item.state = state
        self.session.flush()

    def consume(self, item: InteractionSession) -> None:
        item.consumed_at = utc_now()
        self.session.flush()


class FinanceRepository:
    """Group-scoped constructors; composite FKs enforce tenant boundaries."""

    def __init__(self, session: Session, group_id: int) -> None:
        self.session = session
        self.group_id = group_id

    def add_counterparty(self, display_name: str) -> Counterparty:
        item = Counterparty(group_id=self.group_id, display_name=display_name)
        self.session.add(item)
        self.session.flush()
        return item

    def add_request(
        self,
        *,
        counterparty_id: int,
        requested_at: datetime,
        source_message_id: int | None = None,
        amount_value: int | None = None,
        amount_unit: str = "unknown",
        amount_rial: int | None = None,
    ) -> PaymentRequest:
        item = PaymentRequest(
            group_id=self.group_id,
            counterparty_id=counterparty_id,
            source_message_id=source_message_id,
            requested_at=requested_at,
            amount_value=amount_value,
            amount_unit=amount_unit,
            amount_rial=amount_rial,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def add_payment(
        self,
        *,
        amount_value: int | None,
        amount_unit: str,
        amount_rial: int | None,
    ) -> Payment:
        item = Payment(
            group_id=self.group_id,
            amount_value=amount_value,
            amount_unit=amount_unit,
            amount_rial=amount_rial,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def allocate(self, *, request_id: int, payment_id: int, allocated_rial: int) -> RequestPaymentAllocation:
        item = RequestPaymentAllocation(
            group_id=self.group_id,
            request_id=request_id,
            payment_id=payment_id,
            allocated_rial=allocated_rial,
        )
        self.session.add(item)
        self.session.flush()
        return item


class HistoryRepository:
    """Append/list only. SQLite triggers additionally reject update/delete operations."""

    def __init__(self, session: Session, group_id: int) -> None:
        self.session = session
        self.group_id = group_id

    def append_revision(
        self,
        *,
        target_type: str,
        target_id: int,
        revision_number: int,
        snapshot: dict[str, Any],
        reason: str,
        actor_user_id: int | None = None,
    ) -> RecordRevision:
        item = RecordRevision(
            group_id=self.group_id,
            target_type=target_type,
            target_id=target_id,
            revision_number=revision_number,
            snapshot=snapshot,
            reason=reason,
            actor_user_id=actor_user_id,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def append_audit(
        self,
        *,
        action: str,
        target_type: str,
        target_id: int | None,
        metadata: dict[str, Any] | None = None,
        actor_user_id: int | None = None,
        correlation_id: str | None = None,
    ) -> AuditEvent:
        item = AuditEvent(
            group_id=self.group_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata_json=metadata or {},
            actor_user_id=actor_user_id,
            correlation_id=correlation_id,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def revisions_for(self, target_type: str, target_id: int) -> list[RecordRevision]:
        return list(
            self.session.scalars(
                select(RecordRevision)
                .where(
                    RecordRevision.group_id == self.group_id,
                    RecordRevision.target_type == target_type,
                    RecordRevision.target_id == target_id,
                )
                .order_by(RecordRevision.revision_number)
            )
        )

    def audit_events(self) -> list[AuditEvent]:
        return list(
            self.session.scalars(
                select(AuditEvent)
                .where(AuditEvent.group_id == self.group_id)
                .order_by(AuditEvent.id)
            )
        )


class ExtractionAttemptRepository:
    """Append-only storage for completed provider observations and safe failures."""

    SCHEMA_VERSION = "receipt-v3"

    def __init__(self, session: Session, group_id: int) -> None:
        self.session = session
        self.group_id = group_id

    def _resolve_evidence(
        self, *, evidence_id: int | None, receipt_id: int | None
    ) -> PaymentEvidence:
        if evidence_id is not None:
            evidence = self.session.scalar(
                select(PaymentEvidence).where(
                    PaymentEvidence.id == evidence_id,
                    PaymentEvidence.group_id == self.group_id,
                )
            )
            if evidence is None:
                raise LookupError("payment evidence does not belong to this group")
            return evidence
        if receipt_id is None:
            raise ValueError("evidence_id or receipt_id is required")
        existing = self.session.scalar(
            select(PaymentEvidence)
            .where(
                PaymentEvidence.group_id == self.group_id,
                PaymentEvidence.receipt_image_id == receipt_id,
            )
            .order_by(PaymentEvidence.id)
            .limit(1)
        )
        if existing is not None:
            return existing
        receipt = self.session.get(ReceiptImage, receipt_id)
        if receipt is None:
            raise LookupError("receipt image does not exist")
        evidence = PaymentEvidence(
            group_id=self.group_id,
            source_message_id=receipt.source_message_id,
            evidence_kind=EvidenceKind.IMAGE.value,
            receipt_image_id=receipt.id,
            content_sha256=receipt.sha256,
        )
        self.session.add(evidence)
        self.session.flush()
        return evidence

    def _next_attempt_number(self, evidence_id: int) -> int:
        latest = self.session.scalar(
            select(ExtractionAttempt.attempt_number)
            .where(
                ExtractionAttempt.group_id == self.group_id,
                ExtractionAttempt.evidence_id == evidence_id,
            )
            .order_by(ExtractionAttempt.attempt_number.desc())
            .limit(1)
        )
        return (latest or 0) + 1

    def count_for_evidence(self, evidence_id: int) -> int:
        return int(
            self.session.scalar(
                select(func.count(ExtractionAttempt.id)).where(
                    ExtractionAttempt.group_id == self.group_id,
                    ExtractionAttempt.evidence_id == evidence_id,
                )
            )
            or 0
        )

    def append_success(
        self,
        *,
        outcome: ExtractionOutcome,
        evidence_id: int | None = None,
        receipt_id: int | None = None,
    ) -> ExtractionAttempt:
        evidence = self._resolve_evidence(evidence_id=evidence_id, receipt_id=receipt_id)
        normalized = outcome.normalized
        uncertainty = {
            "fields": [field.value for field in normalized.uncertain_fields],
            "evidence": [item.model_dump(mode="json") for item in normalized.evidence],
        }
        item = ExtractionAttempt(
            group_id=self.group_id,
            evidence_id=evidence.id,
            receipt_id=evidence.receipt_image_id,
            attempt_number=self._next_attempt_number(evidence.id),
            provider=outcome.provider_result.provider,
            model=outcome.provider_result.model,
            schema_version=self.SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            status="succeeded",
            raw_response=outcome.provider_result.raw_response,
            normalized_candidate=normalized.model_dump(mode="json"),
            validation_issues=[issue.model_dump(mode="json") for issue in normalized.validation_issues],
            uncertainty=uncertainty,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def append_failure(
        self,
        *,
        provider: str,
        model: str,
        error_code: str,
        retryable: bool,
        evidence_id: int | None = None,
        receipt_id: int | None = None,
    ) -> ExtractionAttempt:
        evidence = self._resolve_evidence(evidence_id=evidence_id, receipt_id=receipt_id)
        item = ExtractionAttempt(
            group_id=self.group_id,
            evidence_id=evidence.id,
            receipt_id=evidence.receipt_image_id,
            attempt_number=self._next_attempt_number(evidence.id),
            provider=provider,
            model=model,
            schema_version=self.SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            status="failed_retryable" if retryable else "failed_final",
            raw_response=None,
            normalized_candidate=None,
            validation_issues=[],
            uncertainty={},
            error_class=error_code,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def list_for_receipt(self, receipt_id: int) -> list[ExtractionAttempt]:
        return list(
            self.session.scalars(
                select(ExtractionAttempt)
                .where(
                    ExtractionAttempt.group_id == self.group_id,
                    ExtractionAttempt.receipt_id == receipt_id,
                )
                .order_by(ExtractionAttempt.attempt_number)
            )
        )

    def list_for_evidence(self, evidence_id: int) -> list[ExtractionAttempt]:
        return list(
            self.session.scalars(
                select(ExtractionAttempt)
                .where(
                    ExtractionAttempt.group_id == self.group_id,
                    ExtractionAttempt.evidence_id == evidence_id,
                )
                .order_by(ExtractionAttempt.attempt_number)
            )
        )
