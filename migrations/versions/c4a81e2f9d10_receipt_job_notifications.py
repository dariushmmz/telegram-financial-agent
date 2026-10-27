"""add durable receipt processing notification markers

Revision ID: c4a81e2f9d10
Revises: b6d91f4a2c38
Create Date: 2026-10-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a81e2f9d10"
down_revision: str | Sequence[str] | None = "b6d91f4a2c38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "receipt_jobs",
        sa.Column("delayed_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "receipt_jobs",
        sa.Column("failure_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "text_receipt_jobs",
        sa.Column("delayed_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "text_receipt_jobs",
        sa.Column("failure_notified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("text_receipt_jobs", "failure_notified_at")
    op.drop_column("text_receipt_jobs", "delayed_notified_at")
    op.drop_column("receipt_jobs", "failure_notified_at")
    op.drop_column("receipt_jobs", "delayed_notified_at")
