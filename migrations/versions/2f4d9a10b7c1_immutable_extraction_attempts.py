"""make extraction attempts append only

Revision ID: 2f4d9a10b7c1
Revises: 914ed51acf70
Create Date: 2026-10-20
"""
from typing import Sequence, Union

from alembic import op


revision: str = "2f4d9a10b7c1"
down_revision: Union[str, Sequence[str], None] = "914ed51acf70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_extraction_attempts_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_extraction_attempts_no_update")
