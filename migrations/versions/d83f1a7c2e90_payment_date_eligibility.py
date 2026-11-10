"""add receipt transaction date eligibility metadata

Revision ID: d83f1a7c2e90
Revises: c4a81e2f9d10
Create Date: 2026-11-04
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d83f1a7c2e90"
down_revision: str | Sequence[str] | None = "c4a81e2f9d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("corrected_transaction_at", sa.String(128)))
    op.add_column("payments", sa.Column("parsed_transaction_at", sa.DateTime(timezone=True)))
    op.add_column("payments", sa.Column("transaction_calendar", sa.String(16)))
    op.add_column(
        "payments",
        sa.Column(
            "date_eligibility_state",
            sa.String(32),
            nullable=False,
            server_default="legacy_unassessed",
        ),
    )
    op.add_column("payments", sa.Column("date_assessed_at", sa.DateTime(timezone=True)))
    op.add_column("payments", sa.Column("date_max_age_days", sa.Integer()))
    op.execute(
        "CREATE TRIGGER trg_allocation_date_insert_guard BEFORE INSERT ON request_payment_allocations BEGIN "
        "SELECT CASE WHEN (SELECT date_eligibility_state FROM payments "
        "WHERE id=NEW.payment_id AND group_id=NEW.group_id) NOT IN "
        "('valid','approved_exception','legacy_unassessed') "
        "THEN RAISE(ABORT, 'payment date is not financially eligible') END; END"
    )
    op.execute(
        "CREATE TRIGGER trg_allocation_date_update_guard BEFORE UPDATE OF payment_id,group_id,state ON request_payment_allocations BEGIN "
        "SELECT CASE WHEN NEW.state IN ('proposed','confirmed') AND "
        "(SELECT date_eligibility_state FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) "
        "NOT IN ('valid','approved_exception','legacy_unassessed') "
        "THEN RAISE(ABORT, 'payment date is not financially eligible') END; END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_allocation_date_update_guard")
    op.execute("DROP TRIGGER IF EXISTS trg_allocation_date_insert_guard")
    op.drop_column("payments", "date_max_age_days")
    op.drop_column("payments", "date_assessed_at")
    op.drop_column("payments", "date_eligibility_state")
    op.drop_column("payments", "transaction_calendar")
    op.drop_column("payments", "parsed_transaction_at")
    op.drop_column("payments", "corrected_transaction_at")
