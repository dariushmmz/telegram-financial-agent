"""add reconciliation duplicate and revision fields

Revision ID: b6d91f4a2c38
Revises: 8e4a1c2d7b90
Create Date: 2026-10-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6d91f4a2c38"
down_revision: Union[str, Sequence[str], None] = "8e4a1c2d7b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("duplicate_of_payment_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("payments") as batch_op:
        batch_op.create_foreign_key(
            op.f("fk_payments_duplicate_of_payment_id_payments"),
            "payments",
            ["duplicate_of_payment_id", "group_id"],
            ["id", "group_id"],
            ondelete="RESTRICT",
        )

    op.add_column(
        "request_payment_allocations",
        sa.Column("revision_number", sa.Integer(), nullable=False, server_default="1"),
    )
    with op.batch_alter_table("request_payment_allocations") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_request_payment_allocations_allocation_positive_revision"),
            "revision_number >= 1",
        )

    op.add_column(
        "payment_promises",
        sa.Column("revision_number", sa.Integer(), nullable=False, server_default="1"),
    )
    with op.batch_alter_table("payment_promises") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_payment_promises_promise_positive_revision"),
            "revision_number >= 1",
        )

    op.execute(
        "CREATE TRIGGER trg_allocation_insert_guard "
        "BEFORE INSERT ON request_payment_allocations "
        "WHEN NEW.state IN ('proposed', 'confirmed') BEGIN "
        "SELECT CASE WHEN (SELECT amount_rial FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) IS NULL "
        "THEN RAISE(ABORT, 'payment amount is not comparable') END; "
        "SELECT CASE WHEN (SELECT amount_rial FROM payment_requests WHERE id=NEW.request_id AND group_id=NEW.group_id) IS NULL "
        "THEN RAISE(ABORT, 'request amount is not comparable') END; "
        "SELECT CASE WHEN (SELECT evidence_state FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) IN ('duplicate', 'rejected') "
        "THEN RAISE(ABORT, 'payment is not allocatable') END; "
        "SELECT CASE WHEN (SELECT state FROM payment_requests WHERE id=NEW.request_id AND group_id=NEW.group_id) IN ('closed', 'cancelled') "
        "THEN RAISE(ABORT, 'request is not allocatable') END; "
        "SELECT CASE WHEN NEW.state='confirmed' AND (SELECT evidence_state FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) <> 'confirmed' "
        "THEN RAISE(ABORT, 'payment evidence is not confirmed') END; "
        "SELECT CASE WHEN (SELECT counterparty_id FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) IS NOT NULL "
        "AND (SELECT counterparty_id FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) <> "
        "(SELECT counterparty_id FROM payment_requests WHERE id=NEW.request_id AND group_id=NEW.group_id) "
        "THEN RAISE(ABORT, 'allocation counterparty mismatch') END; "
        "SELECT CASE WHEN COALESCE((SELECT SUM(allocated_rial) FROM request_payment_allocations "
        "WHERE group_id=NEW.group_id AND payment_id=NEW.payment_id AND state IN ('proposed','confirmed')),0) + NEW.allocated_rial > "
        "(SELECT amount_rial FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) "
        "THEN RAISE(ABORT, 'allocation exceeds payment') END; "
        "SELECT CASE WHEN COALESCE((SELECT SUM(allocated_rial) FROM request_payment_allocations "
        "WHERE group_id=NEW.group_id AND request_id=NEW.request_id AND state IN ('proposed','confirmed')),0) + NEW.allocated_rial > "
        "(SELECT amount_rial FROM payment_requests WHERE id=NEW.request_id AND group_id=NEW.group_id) "
        "THEN RAISE(ABORT, 'allocation exceeds request') END; END"
    )
    op.execute(
        "CREATE TRIGGER trg_allocation_update_guard "
        "BEFORE UPDATE ON request_payment_allocations "
        "WHEN NEW.state IN ('proposed', 'confirmed') BEGIN "
        "SELECT CASE WHEN (SELECT amount_rial FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) IS NULL "
        "THEN RAISE(ABORT, 'payment amount is not comparable') END; "
        "SELECT CASE WHEN (SELECT amount_rial FROM payment_requests WHERE id=NEW.request_id AND group_id=NEW.group_id) IS NULL "
        "THEN RAISE(ABORT, 'request amount is not comparable') END; "
        "SELECT CASE WHEN (SELECT evidence_state FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) IN ('duplicate', 'rejected') "
        "THEN RAISE(ABORT, 'payment is not allocatable') END; "
        "SELECT CASE WHEN (SELECT state FROM payment_requests WHERE id=NEW.request_id AND group_id=NEW.group_id) IN ('closed', 'cancelled') "
        "THEN RAISE(ABORT, 'request is not allocatable') END; "
        "SELECT CASE WHEN NEW.state='confirmed' AND (SELECT evidence_state FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) <> 'confirmed' "
        "THEN RAISE(ABORT, 'payment evidence is not confirmed') END; "
        "SELECT CASE WHEN (SELECT counterparty_id FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) IS NOT NULL "
        "AND (SELECT counterparty_id FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) <> "
        "(SELECT counterparty_id FROM payment_requests WHERE id=NEW.request_id AND group_id=NEW.group_id) "
        "THEN RAISE(ABORT, 'allocation counterparty mismatch') END; "
        "SELECT CASE WHEN COALESCE((SELECT SUM(allocated_rial) FROM request_payment_allocations "
        "WHERE group_id=NEW.group_id AND payment_id=NEW.payment_id AND state IN ('proposed','confirmed') AND id<>OLD.id),0) + NEW.allocated_rial > "
        "(SELECT amount_rial FROM payments WHERE id=NEW.payment_id AND group_id=NEW.group_id) "
        "THEN RAISE(ABORT, 'allocation exceeds payment') END; "
        "SELECT CASE WHEN COALESCE((SELECT SUM(allocated_rial) FROM request_payment_allocations "
        "WHERE group_id=NEW.group_id AND request_id=NEW.request_id AND state IN ('proposed','confirmed') AND id<>OLD.id),0) + NEW.allocated_rial > "
        "(SELECT amount_rial FROM payment_requests WHERE id=NEW.request_id AND group_id=NEW.group_id) "
        "THEN RAISE(ABORT, 'allocation exceeds request') END; END"
    )
    op.execute(
        "CREATE TRIGGER trg_payment_allocation_guard BEFORE UPDATE ON payments BEGIN "
        "SELECT CASE WHEN COALESCE((SELECT SUM(allocated_rial) FROM request_payment_allocations "
        "WHERE group_id=NEW.group_id AND payment_id=NEW.id AND state IN ('proposed','confirmed')),0) > COALESCE(NEW.amount_rial,-1) "
        "THEN RAISE(ABORT, 'payment amount below allocations') END; "
        "SELECT CASE WHEN NEW.evidence_state IN ('duplicate','rejected') AND EXISTS "
        "(SELECT 1 FROM request_payment_allocations WHERE group_id=NEW.group_id AND payment_id=NEW.id AND state IN ('proposed','confirmed')) "
        "THEN RAISE(ABORT, 'inactive payment has active allocations') END; END"
    )
    op.execute(
        "CREATE TRIGGER trg_request_allocation_guard BEFORE UPDATE ON payment_requests BEGIN "
        "SELECT CASE WHEN COALESCE((SELECT SUM(allocated_rial) FROM request_payment_allocations "
        "WHERE group_id=NEW.group_id AND request_id=NEW.id AND state IN ('proposed','confirmed')),0) > COALESCE(NEW.amount_rial,-1) "
        "THEN RAISE(ABORT, 'request amount below allocations') END; "
        "SELECT CASE WHEN NEW.state IN ('closed','cancelled') AND EXISTS "
        "(SELECT 1 FROM request_payment_allocations WHERE group_id=NEW.group_id AND request_id=NEW.id AND state IN ('proposed','confirmed')) "
        "THEN RAISE(ABORT, 'inactive request has active allocations') END; END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_request_allocation_guard")
    op.execute("DROP TRIGGER IF EXISTS trg_payment_allocation_guard")
    op.execute("DROP TRIGGER IF EXISTS trg_allocation_update_guard")
    op.execute("DROP TRIGGER IF EXISTS trg_allocation_insert_guard")
    with op.batch_alter_table("payment_promises") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_payment_promises_promise_positive_revision"), type_="check"
        )
        batch_op.drop_column("revision_number")

    with op.batch_alter_table("request_payment_allocations") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_request_payment_allocations_allocation_positive_revision"), type_="check"
        )
        batch_op.drop_column("revision_number")

    with op.batch_alter_table("payments") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_payments_duplicate_of_payment_id_payments"), type_="foreignkey"
        )
        batch_op.drop_column("duplicate_of_payment_id")
