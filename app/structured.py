"""Shared contracts for versioned structured records and their read stores."""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from app.models import DateOnly, EvidenceItem, EvidenceSource, Money, StrictModel

DEMO_STRUCTURED_SNAPSHOT_ID = "STRUCTURED-DATA-DEMO-1.0.0"


class StructuredRecord(StrictModel):
    fixture_id: str = Field(min_length=1)
    query_type: str = Field(min_length=1)
    record_key: str = Field(min_length=1)
    snapshot_version: str = Field(min_length=1)
    effective_at: DateOnly
    value: str | int | bool
    consumer: str = Field(min_length=1)
    available_amount: Money | None = None


class StructuredRecordFinder(Protocol):
    def find(
        self,
        *,
        query_type: str,
        record_key: str,
        snapshot_ids: set[str],
        as_of: DateOnly,
        request_id: str | None = None,
    ) -> tuple[str, StructuredRecord] | None: ...


def structured_record_to_evidence(
    snapshot_id: str, record: StructuredRecord
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"STRUCT:{snapshot_id}:{record.fixture_id}",
        source_type=EvidenceSource.STRUCTURED_RECORD,
        structured_data_snapshot_id=snapshot_id,
        fixture_id=record.fixture_id,
        query_type=record.query_type,
        record_key=record.record_key,
        snapshot_version=record.snapshot_version,
        effective_at=record.effective_at,
        value=record.value,
        available_amount=record.available_amount,
        consumer=record.consumer,
    )
