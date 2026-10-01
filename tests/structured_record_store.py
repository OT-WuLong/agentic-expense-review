"""Fixture-backed record finder for structured lookup tests."""

from pathlib import Path

from pydantic import Field

from app.models import DateOnly, StrictModel
from app.structured import StructuredRecord


class StructuredFixture(StrictModel):
    schema_version: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    records: list[StructuredRecord]
    created_on: DateOnly | None = None
    updated_on: DateOnly | None = None
    source_type: str | None = None
    license_id: str | None = None

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = (
    ROOT / "data/fixtures/structured_records.json",
    ROOT / "data/fixtures/structured_records_demo.json",
    ROOT / "data/fixtures/structured_records_reference.json",
    ROOT / "data/fixtures/p06_structured_lookup.json",
)


class StructuredRecordStore:
    def __init__(self, paths: tuple[Path, ...] = DEFAULT_FIXTURES) -> None:
        self.records: list[tuple[str, StructuredRecord]] = []
        seen: set[tuple[str, str]] = set()
        for path in paths:
            fixture = StructuredFixture.model_validate_json(path.read_text(encoding="utf-8"))
            for record in fixture.records:
                identity = (fixture.snapshot_id, record.fixture_id)
                if identity in seen:
                    raise ValueError(f"duplicate structured fixture: {identity}")
                seen.add(identity)
                self.records.append((fixture.snapshot_id, record))

    def find(
        self,
        *,
        query_type: str,
        record_key: str,
        snapshot_ids: set[str],
        as_of: DateOnly,
        request_id: str | None = None,
    ) -> tuple[str, StructuredRecord] | None:
        del request_id
        matches = [
            (snapshot_id, record)
            for snapshot_id, record in self.records
            if snapshot_id in snapshot_ids
            and record.query_type == query_type
            and record.record_key == record_key
            and record.effective_at <= as_of
        ]
        return max(matches, key=lambda item: (item[1].effective_at, item[1].snapshot_version)) if matches else None
