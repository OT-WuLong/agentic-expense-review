"""Create the P08 approval, rule, audit, and structured-record tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260920_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("request_id", sa.Text(), primary_key=True),
        sa.Column("employee_id", sa.Text(), nullable=False),
        sa.Column("department_id", sa.Text(), nullable=False),
        sa.Column("expense_type", sa.Text(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("submitted_on", sa.Date(), nullable=False),
        sa.Column("application", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="CREATED"),
        sa.Column("recommendation", sa.Text()),
        sa.Column("risk_level", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "document_metadata",
        sa.Column("document_id", sa.Text(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Text(),
            sa.ForeignKey("approval_requests.request_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("storage_uri", sa.Text()),
        sa.Column("sha256", sa.String(length=64)),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "extracted_fields",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Text(),
            sa.ForeignKey("approval_requests.request_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("raw_value", sa.Text()),
        sa.Column("document_id", sa.Text()),
        sa.Column("page", sa.Integer()),
        sa.UniqueConstraint(
            "request_id", "field_name", "document_id", name="uq_extracted_field_source"
        ),
    )
    op.create_table(
        "policy_rules",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("expense_type", sa.Text()),
        sa.Column("rule_type", sa.Text(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_document_id", sa.Text()),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint(
            "rule_id", "rule_version", "source_document_id", name="uq_policy_rule_version_source"
        ),
    )
    op.create_index(
        "ix_policy_rules_lookup",
        "policy_rules",
        ["expense_type", "effective_from", "effective_to"],
    )
    op.create_table(
        "structured_records",
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("fixture_id", sa.Text(), nullable=False),
        sa.Column("query_type", sa.Text(), nullable=False),
        sa.Column("record_key", sa.Text(), nullable=False),
        sa.Column("snapshot_version", sa.Text(), nullable=False),
        sa.Column("effective_at", sa.Date(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("available_amount", sa.Numeric(14, 2)),
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id", "fixture_id"),
    )
    op.create_index(
        "ix_structured_records_lookup",
        "structured_records",
        ["snapshot_id", "query_type", "record_key", sa.text("effective_at DESC")],
    )
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("request_id", sa.Text()),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.Text(), unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_index("ix_structured_records_lookup", table_name="structured_records")
    op.drop_table("structured_records")
    op.drop_index("ix_policy_rules_lookup", table_name="policy_rules")
    op.drop_table("policy_rules")
    op.drop_table("extracted_fields")
    op.drop_table("document_metadata")
    op.drop_table("approval_requests")
