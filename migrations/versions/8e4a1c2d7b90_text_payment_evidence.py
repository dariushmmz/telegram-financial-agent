"""add generic payment evidence and text receipt extraction

Revision ID: 8e4a1c2d7b90
Revises: 6c0e9f41d2ab
Create Date: 2026-10-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8e4a1c2d7b90"
down_revision: Union[str, Sequence[str], None] = "6c0e9f41d2ab"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_immutable_extraction_triggers() -> None:
    op.execute(
        "CREATE TRIGGER trg_extraction_attempts_no_update "
        "BEFORE UPDATE ON extraction_attempts BEGIN "
        "SELECT RAISE(ABORT, 'extraction_attempts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_extraction_attempts_no_delete "
        "BEFORE DELETE ON extraction_attempts BEGIN "
        "SELECT RAISE(ABORT, 'extraction_attempts are append-only'); END"
    )


def upgrade() -> None:
    op.create_table(
        "payment_evidence",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), nullable=False),
        sa.Column("evidence_kind", sa.String(length=32), nullable=False),
        sa.Column("receipt_image_id", sa.Integer(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("classification_state", sa.String(length=32), nullable=False),
        sa.Column("duplicate_of_evidence_id", sa.Integer(), nullable=True),
        sa.Column("duplicate_disposition", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name=op.f("ck_payment_evidence_evidence_sha256_length"),
        ),
        sa.CheckConstraint(
            "(evidence_kind = 'image' AND receipt_image_id IS NOT NULL) OR "
            "(evidence_kind = 'telegram_text' AND receipt_image_id IS NULL)",
            name=op.f("ck_payment_evidence_evidence_kind_payload"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["telegram_groups.id"],
            name=op.f("fk_payment_evidence_group_id_telegram_groups"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id", "group_id"],
            ["telegram_messages.id", "telegram_messages.group_id"],
            name=op.f("fk_payment_evidence_source_message_id_telegram_messages"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_image_id", "group_id"],
            ["receipt_images.id", "receipt_images.group_id"],
            name=op.f("fk_payment_evidence_receipt_image_id_receipt_images"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_evidence_id", "group_id"],
            ["payment_evidence.id", "payment_evidence.group_id"],
            name=op.f("fk_payment_evidence_duplicate_of_evidence_id_payment_evidence"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_evidence")),
        sa.UniqueConstraint("id", "group_id", name="uq_payment_evidence_id_group"),
        sa.UniqueConstraint("source_message_id", name="uq_payment_evidence_source_message"),
    )
    op.create_index("ix_evidence_group_created", "payment_evidence", ["group_id", "created_at"])
    op.create_index(
        "ix_evidence_group_hash", "payment_evidence", ["group_id", "evidence_kind", "content_sha256"]
    )

    op.execute(
        "INSERT INTO payment_evidence "
        "(group_id, source_message_id, evidence_kind, receipt_image_id, content_sha256, "
        "classification_state, duplicate_disposition, created_at) "
        "SELECT group_id, source_message_id, 'image', id, sha256, 'receipt_candidate', "
        "duplicate_disposition, created_at FROM receipt_images"
    )
    op.execute(
        "UPDATE payment_evidence SET duplicate_of_evidence_id = ("
        "SELECT parent.id FROM receipt_images child "
        "JOIN payment_evidence parent ON parent.receipt_image_id = child.duplicate_of_receipt_id "
        "WHERE child.id = payment_evidence.receipt_image_id"
        ") WHERE receipt_image_id IN (SELECT id FROM receipt_images WHERE duplicate_of_receipt_id IS NOT NULL)"
    )
    # Older file-identity duplicate jobs can point at a canonical image while having a distinct source message.
    op.execute(
        "INSERT INTO payment_evidence "
        "(group_id, source_message_id, evidence_kind, receipt_image_id, content_sha256, "
        "classification_state, duplicate_of_evidence_id, duplicate_disposition, created_at) "
        "SELECT j.group_id, j.source_message_id, 'image', r.id, r.sha256, 'receipt_candidate', "
        "canonical.id, 'exact', j.created_at FROM receipt_jobs j "
        "JOIN receipt_images r ON r.id = j.receipt_id AND r.group_id = j.group_id "
        "JOIN payment_evidence canonical ON canonical.receipt_image_id = r.id "
        "WHERE NOT EXISTS (SELECT 1 FROM payment_evidence e WHERE e.source_message_id = j.source_message_id)"
    )

    op.add_column("receipt_jobs", sa.Column("evidence_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE receipt_jobs SET evidence_id = (SELECT e.id FROM payment_evidence e "
        "WHERE e.source_message_id = receipt_jobs.source_message_id)"
    )
    with op.batch_alter_table("receipt_jobs") as batch_op:
        batch_op.create_foreign_key(
            op.f("fk_receipt_jobs_evidence_id_payment_evidence"),
            "payment_evidence", ["evidence_id", "group_id"], ["id", "group_id"], ondelete="RESTRICT",
        )

    op.execute("DROP TRIGGER IF EXISTS trg_extraction_attempts_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_extraction_attempts_no_update")
    op.add_column("extraction_attempts", sa.Column("evidence_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE extraction_attempts SET evidence_id = (SELECT e.id FROM payment_evidence e "
        "WHERE e.receipt_image_id = extraction_attempts.receipt_id ORDER BY e.id LIMIT 1)"
    )
    with op.batch_alter_table("extraction_attempts") as batch_op:
        batch_op.alter_column("evidence_id", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("receipt_id", existing_type=sa.Integer(), nullable=True)
        batch_op.drop_constraint("uq_extraction_receipt_attempt", type_="unique")
        batch_op.create_unique_constraint(
            "uq_extraction_evidence_attempt", ["evidence_id", "attempt_number"]
        )
        batch_op.create_foreign_key(
            op.f("fk_extraction_attempts_evidence_id_payment_evidence"),
            "payment_evidence", ["evidence_id", "group_id"], ["id", "group_id"], ondelete="RESTRICT",
        )
    _create_immutable_extraction_triggers()

    op.add_column("payments", sa.Column("evidence_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE payments SET evidence_id = (SELECT e.id FROM payment_evidence e "
        "WHERE e.receipt_image_id = payments.receipt_id ORDER BY e.id LIMIT 1) "
        "WHERE receipt_id IS NOT NULL"
    )
    with op.batch_alter_table("payments") as batch_op:
        batch_op.create_unique_constraint("uq_payment_evidence", ["evidence_id"])
        batch_op.create_foreign_key(
            op.f("fk_payments_evidence_id_payment_evidence"),
            "payment_evidence", ["evidence_id", "group_id"], ["id", "group_id"], ondelete="RESTRICT",
        )

    op.create_table(
        "text_receipt_jobs",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), nullable=False),
        sa.Column("evidence_id", sa.Integer(), nullable=False),
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
            "attempt_count >= 0", name=op.f("ck_text_receipt_jobs_text_receipt_job_nonnegative_attempts")
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["telegram_groups.id"],
            name=op.f("fk_text_receipt_jobs_group_id_telegram_groups"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id", "group_id"],
            ["telegram_messages.id", "telegram_messages.group_id"],
            name=op.f("fk_text_receipt_jobs_source_message_id_telegram_messages"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id", "group_id"],
            ["payment_evidence.id", "payment_evidence.group_id"],
            name=op.f("fk_text_receipt_jobs_evidence_id_payment_evidence"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_text_receipt_jobs")),
        sa.UniqueConstraint("evidence_id", name="uq_text_receipt_job_evidence"),
        sa.UniqueConstraint("id", "group_id", name="uq_text_receipt_job_id_group"),
        sa.UniqueConstraint("source_message_id", name="uq_text_receipt_job_source_message"),
    )
    op.create_index(
        "ix_text_receipt_job_queue", "text_receipt_jobs", ["status", "available_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_text_receipt_job_queue", table_name="text_receipt_jobs")
    op.drop_table("text_receipt_jobs")

    with op.batch_alter_table("payments") as batch_op:
        batch_op.drop_constraint(op.f("fk_payments_evidence_id_payment_evidence"), type_="foreignkey")
        batch_op.drop_constraint("uq_payment_evidence", type_="unique")
        batch_op.drop_column("evidence_id")

    op.execute("DROP TRIGGER IF EXISTS trg_extraction_attempts_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_extraction_attempts_no_update")
    with op.batch_alter_table("extraction_attempts") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_extraction_attempts_evidence_id_payment_evidence"), type_="foreignkey"
        )
        batch_op.drop_constraint("uq_extraction_evidence_attempt", type_="unique")
        batch_op.alter_column("receipt_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint("uq_extraction_receipt_attempt", ["receipt_id", "attempt_number"])
        batch_op.drop_column("evidence_id")
    _create_immutable_extraction_triggers()

    with op.batch_alter_table("receipt_jobs") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_receipt_jobs_evidence_id_payment_evidence"), type_="foreignkey"
        )
        batch_op.drop_column("evidence_id")

    op.drop_index("ix_evidence_group_hash", table_name="payment_evidence")
    op.drop_index("ix_evidence_group_created", table_name="payment_evidence")
    op.drop_table("payment_evidence")
