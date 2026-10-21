from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from receipt_bot.domain.enums import (
    CurrencyUnit,
    EvidenceKind,
    ExtractionState,
    MembershipRole,
    PaymentEvidenceState,
    PromiseState,
    RequestState,
    SettlementState,
    StorageState,
    TelegramUpdateState,
    ReceiptJobState,
)
from receipt_bot.domain.time import utc_now


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IdMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class TelegramGroup(IdMixin, TimestampMixin, Base):
    __tablename__ = "telegram_groups"

    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Tehran")
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=183)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    settings_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (CheckConstraint("retention_days >= 1", name="positive_retention_days"),)


class TelegramUser(IdMixin, TimestampMixin, Base):
    __tablename__ = "telegram_users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))


class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"))
    update_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TelegramUpdateState.RECEIVED.value
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="telegram_update_nonnegative_attempts"),
        Index("ix_telegram_update_status", "status", "update_id"),
    )


class GroupMembership(Base):
    __tablename__ = "group_memberships"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=MembershipRole.MEMBER.value)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    granted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    revoked_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramMessage(IdMixin, Base):
    __tablename__ = "telegram_messages"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_thread_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_update_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    sender_user_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"))
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    ingestion_state: Mapped[str] = mapped_column(String(32), nullable=False, default="captured")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("group_id", "telegram_message_id", name="uq_message_group_telegram_id"),
        UniqueConstraint("id", "group_id", name="uq_message_id_group"),
        Index("ix_message_group_occurred", "group_id", "occurred_at"),
    )


class Counterparty(IdMixin, TimestampMixin, Base):
    __tablename__ = "counterparties"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    telegram_user_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"))
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (UniqueConstraint("id", "group_id", name="uq_counterparty_id_group"),)


class PaymentRequest(IdMixin, TimestampMixin, Base):
    __tablename__ = "payment_requests"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    counterparty_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(Integer)
    amount_value: Mapped[int | None] = mapped_column(BigInteger)
    amount_unit: Mapped[str] = mapped_column(String(16), nullable=False, default=CurrencyUnit.UNKNOWN.value)
    amount_rial: Mapped[int | None] = mapped_column(BigInteger)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=RequestState.DRAFT.value)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"))
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("id", "group_id", name="uq_request_id_group"),
        ForeignKeyConstraint(
            ["counterparty_id", "group_id"], ["counterparties.id", "counterparties.group_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["source_message_id", "group_id"],
            ["telegram_messages.id", "telegram_messages.group_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount_value IS NULL OR amount_value >= 0", name="request_nonnegative_source_amount"),
        CheckConstraint("amount_rial IS NULL OR amount_rial >= 0", name="request_nonnegative_rial_amount"),
        CheckConstraint("revision_number >= 1", name="request_positive_revision"),
        Index("ix_request_group_requested", "group_id", "requested_at"),
    )


class ReceiptImage(IdMixin, Base):
    __tablename__ = "receipt_images"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    source_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    telegram_file_id: Mapped[str] = mapped_column(String(512), nullable=False)
    telegram_file_unique_id: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(127), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    perceptual_hash: Mapped[str | None] = mapped_column(String(16))
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    storage_state: Mapped[str] = mapped_column(String(32), nullable=False, default=StorageState.PRESENT.value)
    duplicate_of_receipt_id: Mapped[int | None] = mapped_column(Integer)
    duplicate_disposition: Mapped[str | None] = mapped_column(String(32))
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "group_id", name="uq_receipt_id_group"),
        UniqueConstraint("group_id", "telegram_file_unique_id", name="uq_receipt_group_file_unique"),
        ForeignKeyConstraint(
            ["source_message_id", "group_id"],
            ["telegram_messages.id", "telegram_messages.group_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["duplicate_of_receipt_id", "group_id"],
            ["receipt_images.id", "receipt_images.group_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("byte_size >= 0", name="receipt_nonnegative_size"),
        Index("ix_receipt_group_sha256", "group_id", "sha256"),
        Index("ix_receipt_group_perceptual_hash", "group_id", "perceptual_hash"),
        Index("ix_receipt_group_created", "group_id", "created_at"),
    )


class PaymentEvidence(IdMixin, Base):
    __tablename__ = "payment_evidence"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    source_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    receipt_image_id: Mapped[int | None] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    classification_state: Mapped[str] = mapped_column(String(32), nullable=False, default="receipt_candidate")
    duplicate_of_evidence_id: Mapped[int | None] = mapped_column(Integer)
    duplicate_disposition: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("id", "group_id", name="uq_payment_evidence_id_group"),
        UniqueConstraint("source_message_id", name="uq_payment_evidence_source_message"),
        ForeignKeyConstraint(
            ["source_message_id", "group_id"],
            ["telegram_messages.id", "telegram_messages.group_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["receipt_image_id", "group_id"],
            ["receipt_images.id", "receipt_images.group_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["duplicate_of_evidence_id", "group_id"],
            ["payment_evidence.id", "payment_evidence.group_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(evidence_kind = 'image' AND receipt_image_id IS NOT NULL) OR "
            "(evidence_kind = 'telegram_text' AND receipt_image_id IS NULL)",
            name="evidence_kind_payload",
        ),
        CheckConstraint("length(content_sha256) = 64", name="evidence_sha256_length"),
        Index("ix_evidence_group_hash", "group_id", "evidence_kind", "content_sha256"),
        Index("ix_evidence_group_created", "group_id", "created_at"),
    )


class ReceiptJob(IdMixin, Base):
    __tablename__ = "receipt_jobs"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    source_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    receipt_id: Mapped[int | None] = mapped_column(Integer)
    evidence_id: Mapped[int | None] = mapped_column(Integer)
    telegram_file_id: Mapped[str] = mapped_column(String(512), nullable=False)
    telegram_file_unique_id: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(512))
    declared_mime_type: Mapped[str | None] = mapped_column(String(127))
    declared_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default=ReceiptJobState.QUEUED_DOWNLOAD.value
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("source_message_id", name="uq_receipt_job_source_message"),
        UniqueConstraint("id", "group_id", name="uq_receipt_job_id_group"),
        ForeignKeyConstraint(
            ["source_message_id", "group_id"],
            ["telegram_messages.id", "telegram_messages.group_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["receipt_id", "group_id"],
            ["receipt_images.id", "receipt_images.group_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["evidence_id", "group_id"],
            ["payment_evidence.id", "payment_evidence.group_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "declared_byte_size IS NULL OR declared_byte_size >= 0",
            name="receipt_job_nonnegative_declared_size",
        ),
        CheckConstraint("attempt_count >= 0", name="receipt_job_nonnegative_attempts"),
        Index("ix_receipt_job_queue", "status", "available_at", "id"),
        Index("ix_receipt_job_group_file_unique", "group_id", "telegram_file_unique_id"),
    )


class TextReceiptJob(IdMixin, Base):
    __tablename__ = "text_receipt_jobs"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    source_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default=ReceiptJobState.QUEUED_EXTRACTION.value
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("source_message_id", name="uq_text_receipt_job_source_message"),
        UniqueConstraint("evidence_id", name="uq_text_receipt_job_evidence"),
        UniqueConstraint("id", "group_id", name="uq_text_receipt_job_id_group"),
        ForeignKeyConstraint(
            ["source_message_id", "group_id"],
            ["telegram_messages.id", "telegram_messages.group_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["evidence_id", "group_id"],
            ["payment_evidence.id", "payment_evidence.group_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("attempt_count >= 0", name="text_receipt_job_nonnegative_attempts"),
        Index("ix_text_receipt_job_queue", "status", "available_at", "id"),
    )


class ExtractionAttempt(IdMixin, Base):
    __tablename__ = "extraction_attempts"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    evidence_id: Mapped[int] = mapped_column(Integer, nullable=False)
    receipt_id: Mapped[int | None] = mapped_column(Integer)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ExtractionState.QUEUED.value)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    normalized_candidate: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    validation_issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    uncertainty: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_class: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("evidence_id", "attempt_number", name="uq_extraction_evidence_attempt"),
        UniqueConstraint("id", "group_id", name="uq_extraction_id_group"),
        ForeignKeyConstraint(
            ["evidence_id", "group_id"], ["payment_evidence.id", "payment_evidence.group_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["receipt_id", "group_id"], ["receipt_images.id", "receipt_images.group_id"], ondelete="RESTRICT"
        ),
        CheckConstraint("attempt_number >= 1", name="extraction_positive_attempt"),
    )


class Payment(IdMixin, TimestampMixin, Base):
    __tablename__ = "payments"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    evidence_id: Mapped[int | None] = mapped_column(Integer)
    duplicate_of_payment_id: Mapped[int | None] = mapped_column(Integer)
    receipt_id: Mapped[int | None] = mapped_column(Integer)
    source_message_id: Mapped[int | None] = mapped_column(Integer)
    counterparty_id: Mapped[int | None] = mapped_column(Integer)
    amount_value: Mapped[int | None] = mapped_column(BigInteger)
    amount_unit: Mapped[str] = mapped_column(String(16), nullable=False, default=CurrencyUnit.UNKNOWN.value)
    amount_rial: Mapped[int | None] = mapped_column(BigInteger)
    printed_transaction_at: Mapped[str | None] = mapped_column(String(128))
    transaction_reference: Mapped[str | None] = mapped_column(String(255))
    evidence_state: Mapped[str] = mapped_column(String(32), nullable=False, default=PaymentEvidenceState.NEEDS_REVIEW.value)
    settlement_state: Mapped[str] = mapped_column(String(32), nullable=False, default=SettlementState.NOT_CHECKED.value)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("id", "group_id", name="uq_payment_id_group"),
        UniqueConstraint("evidence_id", name="uq_payment_evidence"),
        UniqueConstraint("receipt_id", name="uq_payment_receipt"),
        ForeignKeyConstraint(
            ["evidence_id", "group_id"], ["payment_evidence.id", "payment_evidence.group_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["duplicate_of_payment_id", "group_id"], ["payments.id", "payments.group_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["receipt_id", "group_id"], ["receipt_images.id", "receipt_images.group_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["source_message_id", "group_id"],
            ["telegram_messages.id", "telegram_messages.group_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["counterparty_id", "group_id"], ["counterparties.id", "counterparties.group_id"], ondelete="RESTRICT"
        ),
        CheckConstraint("amount_value IS NULL OR amount_value >= 0", name="payment_nonnegative_source_amount"),
        CheckConstraint("amount_rial IS NULL OR amount_rial >= 0", name="payment_nonnegative_rial_amount"),
        CheckConstraint("revision_number >= 1", name="payment_positive_revision"),
        Index("ix_payment_group_created", "group_id", "created_at"),
    )


class RequestPaymentAllocation(IdMixin, Base):
    __tablename__ = "request_payment_allocations"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    request_id: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    allocated_rial: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("request_id", "payment_id", name="uq_allocation_request_payment"),
        ForeignKeyConstraint(
            ["request_id", "group_id"], ["payment_requests.id", "payment_requests.group_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["payment_id", "group_id"], ["payments.id", "payments.group_id"], ondelete="RESTRICT"
        ),
        CheckConstraint("allocated_rial > 0", name="allocation_positive_amount"),
        CheckConstraint("revision_number >= 1", name="allocation_positive_revision"),
    )


class PaymentPromise(IdMixin, TimestampMixin, Base):
    __tablename__ = "payment_promises"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    counterparty_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[int | None] = mapped_column(Integer)
    amount_value: Mapped[int | None] = mapped_column(BigInteger)
    amount_unit: Mapped[str] = mapped_column(String(16), nullable=False, default=CurrencyUnit.UNKNOWN.value)
    amount_rial: Mapped[int | None] = mapped_column(BigInteger)
    expected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=PromiseState.OPEN.value)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("id", "group_id", name="uq_promise_id_group"),
        ForeignKeyConstraint(
            ["counterparty_id", "group_id"], ["counterparties.id", "counterparties.group_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["source_message_id", "group_id"],
            ["telegram_messages.id", "telegram_messages.group_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["request_id", "group_id"], ["payment_requests.id", "payment_requests.group_id"], ondelete="RESTRICT"
        ),
        CheckConstraint("amount_value IS NULL OR amount_value >= 0", name="promise_nonnegative_source_amount"),
        CheckConstraint("amount_rial IS NULL OR amount_rial >= 0", name="promise_nonnegative_rial_amount"),
        CheckConstraint("revision_number >= 1", name="promise_positive_revision"),
    )


class Review(IdMixin, Base):
    __tablename__ = "reviews"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewer_user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    before_revision_id: Mapped[int | None] = mapped_column(Integer)
    after_revision_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (Index("ix_review_group_target", "group_id", "target_type", "target_id"),)


class RecordRevision(IdMixin, Base):
    __tablename__ = "record_revisions"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("group_id", "target_type", "target_id", "revision_number", name="uq_revision_target_number"),
        CheckConstraint("revision_number >= 1", name="revision_positive_number"),
        Index("ix_revision_group_target", "group_id", "target_type", "target_id"),
    )


class AuditEvent(IdMixin, Base):
    __tablename__ = "audit_events"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[int | None] = mapped_column(Integer)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (Index("ix_audit_group_created", "group_id", "created_at"),)


class ReportRun(IdMixin, Base):
    __tablename__ = "report_runs"

    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    report_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_by_user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False)
    calculation_version: Mapped[str] = mapped_column(String(32), nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        CheckConstraint("period_end > period_start", name="report_valid_period"),
        Index("ix_report_group_period", "group_id", "period_start", "period_end"),
    )


class InteractionSession(Base):
    __tablename__ = "interaction_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("telegram_groups.id", ondelete="RESTRICT"), nullable=False)
    initiating_user_id: Mapped[int] = mapped_column(ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False)
    workflow: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (Index("ix_interaction_group_expiry", "group_id", "expires_at"),)
