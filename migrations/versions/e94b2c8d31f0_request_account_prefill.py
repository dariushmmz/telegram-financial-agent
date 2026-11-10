"""add reviewed request account and parser provenance fields

Revision ID: e94b2c8d31f0
Revises: d83f1a7c2e90
Create Date: 2026-11-04
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e94b2c8d31f0"
down_revision: str | Sequence[str] | None = "d83f1a7c2e90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payment_requests", sa.Column("account_holder_name", sa.String(255)))
    op.add_column("payment_requests", sa.Column("destination_account", sa.String(64)))
    op.add_column("payment_requests", sa.Column("destination_card", sa.String(32)))
    op.add_column("payment_requests", sa.Column("destination_iban", sa.String(34)))
    op.add_column("payment_requests", sa.Column("destination_bank", sa.String(128)))
    op.add_column("payment_requests", sa.Column("source_parser_candidate", sa.JSON()))


def downgrade() -> None:
    op.drop_column("payment_requests", "source_parser_candidate")
    op.drop_column("payment_requests", "destination_bank")
    op.drop_column("payment_requests", "destination_iban")
    op.drop_column("payment_requests", "destination_card")
    op.drop_column("payment_requests", "destination_account")
    op.drop_column("payment_requests", "account_holder_name")
