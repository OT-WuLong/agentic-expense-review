"""Persist reviewed case memory and human-gated rule proposals."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260923_0003"
down_revision: str | None = "20260921_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approval_cases",
        sa.Column("case_id", sa.Text(), primary_key=True),
        sa.Column(
            "source_request_id",
            sa.Text(),
            sa.ForeignKey("approval_requests.request_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("department_id", sa.Text(), nullable=False),
        sa.Column("expense_type", sa.Text(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("human_action", sa.Text()),
        sa.Column("rule_refs", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("embedding", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_approval_cases_scope",
        "approval_cases",
        ["department_id", "expense_type", "occurred_on"],
    )
    op.create_table(
        "candidate_rules",
        sa.Column("candidate_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "source_request_id",
            sa.Text(),
            sa.ForeignKey("approval_requests.request_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("target_rule_id", sa.Text(), nullable=False),
        sa.Column("department_id", sa.Text(), nullable=False),
        sa.Column("expense_type", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("reviewer_id", sa.Text()),
        sa.Column("source_document_id", sa.Text()),
        sa.Column("effective_from", sa.Date()),
        sa.Column("replay_cases", postgresql.JSONB()),
        sa.Column("published_rule_row_id", sa.Text()),
        sa.Column("prior_rule_row_id", sa.Text()),
        sa.Column("prior_effective_to", sa.Date()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'SUPERSEDED')",
            name="ck_candidate_rules_status",
        ),
    )
    op.create_index("ix_candidate_rules_status", "candidate_rules", ["status"])


def downgrade() -> None:
    op.drop_index("ix_candidate_rules_status", table_name="candidate_rules")
    op.drop_table("candidate_rules")
    op.drop_index("ix_approval_cases_scope", table_name="approval_cases")
    op.drop_table("approval_cases")
