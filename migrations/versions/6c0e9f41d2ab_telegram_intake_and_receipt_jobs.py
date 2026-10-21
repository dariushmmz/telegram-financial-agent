"""add durable Telegram intake and receipt jobs

Revision ID: 6c0e9f41d2ab
Revises: 2f4d9a10b7c1
Create Date: 2026-10-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6c0e9f41d2ab"
down_revision: Union[str, Sequence[str], None] = "2f4d9a10b7c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_updates",
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("update_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_telegram_updates_telegram_update_nonnegative_attempts")
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["telegram_groups.id"],
            name=op.f("fk_telegram_updates_group_id_telegram_groups"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("update_id", name=op.f("pk_telegram_updates")),
    )
    op.create_index(
        "ix_telegram_update_status", "telegram_updates", ["status", "update_id"], unique=False
    )

    op.add_column("receipt_images", sa.Column("perceptual_hash", sa.String(length=16), nullable=True))
    op.create_index(
        "ix_receipt_group_perceptual_hash",
        "receipt_images",
        ["group_id", "perceptual_hash"],
        unique=False,
    )

    op.create_table(
        "receipt_jobs",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), nullable=False),
        sa.Column("receipt_id", sa.Integer(), nullable=True),
        sa.Column("telegram_file_id", sa.String(length=512), nullable=False),
        sa.Column("telegram_file_unique_id", sa.String(length=255), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=True),
        sa.Column("declared_mime_type", sa.String(length=127), nullable=True),
        sa.Column("declared_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_receipt_jobs_receipt_job_nonnegative_attempts")
        ),
        sa.CheckConstraint(
            "declared_byte_size IS NULL OR declared_byte_size >= 0",
            name=op.f("ck_receipt_jobs_receipt_job_nonnegative_declared_size"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["telegram_groups.id"],
            name=op.f("fk_receipt_jobs_group_id_telegram_groups"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id", "group_id"],
            ["receipt_images.id", "receipt_images.group_id"],
            name=op.f("fk_receipt_jobs_receipt_id_receipt_images"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id", "group_id"],
            ["telegram_messages.id", "telegram_messages.group_id"],
            name=op.f("fk_receipt_jobs_source_message_id_telegram_messages"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_receipt_jobs")),
        sa.UniqueConstraint("id", "group_id", name="uq_receipt_job_id_group"),
        sa.UniqueConstraint("source_message_id", name="uq_receipt_job_source_message"),
    )
    op.create_index(
        "ix_receipt_job_group_file_unique",
        "receipt_jobs",
        ["group_id", "telegram_file_unique_id"],
        unique=False,
    )
    op.create_index(
        "ix_receipt_job_queue",
        "receipt_jobs",
        ["status", "available_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_receipt_job_queue", table_name="receipt_jobs")
    op.drop_index("ix_receipt_job_group_file_unique", table_name="receipt_jobs")
    op.drop_table("receipt_jobs")
    op.drop_index("ix_receipt_group_perceptual_hash", table_name="receipt_images")
    op.drop_column("receipt_images", "perceptual_hash")
    op.drop_index("ix_telegram_update_status", table_name="telegram_updates")
    op.drop_table("telegram_updates")
