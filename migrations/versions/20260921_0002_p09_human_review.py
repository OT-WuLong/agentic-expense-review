"""Add stable workflow identity and human final-decision persistence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260921_0002"
down_revision: str | None = "20260920_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("approval_requests", sa.Column("thread_id", sa.Text()))
    op.add_column(
        "approval_requests", sa.Column("creation_idempotency_key", sa.Text())
    )
    op.add_column(
        "approval_requests",
        sa.Column("final_decision", postgresql.JSONB(astext_type=sa.Text())),
    )
    op.execute(
        """
        UPDATE approval_requests
        SET thread_id = 'approval:' || request_id,
            creation_idempotency_key = 'fixture-create:' || request_id
        """
    )
    op.alter_column("approval_requests", "thread_id", nullable=False)
    op.alter_column(
        "approval_requests", "creation_idempotency_key", nullable=False
    )
    op.create_unique_constraint(
        "uq_approval_requests_thread_id", "approval_requests", ["thread_id"]
    )
    op.create_unique_constraint(
        "uq_approval_requests_creation_idempotency",
        "approval_requests",
        ["creation_idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_approval_requests_creation_idempotency",
        "approval_requests",
        type_="unique",
    )
    op.drop_constraint(
        "uq_approval_requests_thread_id", "approval_requests", type_="unique"
    )
    op.drop_column("approval_requests", "final_decision")
    op.drop_column("approval_requests", "creation_idempotency_key")
    op.drop_column("approval_requests", "thread_id")
