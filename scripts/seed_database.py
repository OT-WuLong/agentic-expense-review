"""Load the existing synthetic fixtures into the local P08 PostgreSQL database."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import DEFAULT_DATABASE_URL

STRUCTURED_FIXTURES = (
    ROOT / "data/fixtures/structured_records.json",
    ROOT / "data/fixtures/structured_records_demo.json",
    ROOT / "data/fixtures/structured_records_reference.json",
    ROOT / "data/fixtures/p06_structured_lookup.json",
)

RULE_FIXTURES = [
    ("scope@1.0", "RULE-EXPENSE-TYPE-IN-SCOPE", None, "scope", {"allowed": ["交通", "住宿", "餐饮"]}, None, "2025-01-01", None),
    ("amount-date@1.0", "RULE-AMOUNT-DATE-CONSISTENCY", "交通", "document_consistency", {}, None, "2025-01-01", None),
    ("transport-purpose@1.0", "RULE-TRANSPORT-PURPOSE-ELIGIBILITY", "交通", "prohibition", {"purpose": "DAILY_COMMUTE"}, "POL-TRANSPORT-2026-V1", "2026-01-01", None),
    ("sales-ticket@1.0", "RULE-SALES-TICKET-CONSISTENCY", "交通", "document_consistency", {}, "POL-DEPT-SALES-TRANSPORT-2026-V1", "2026-01-01", None),
    ("sales-commute@1.0", "RULE-SALES-PRIVATE-COMMUTE", "交通", "prohibition", {}, "POL-DEPT-SALES-TRANSPORT-2026-V1", "2026-01-01", None),
    ("sales-taxi-limit@1.0", "RULE-SALES-TAXI-LIMIT", "交通", "amount_limit", {"limit": "260.00"}, "POL-DEPT-SALES-TRANSPORT-2026-V1", "2026-01-01", None),
    ("sales-customer-visit@1.0", "RULE-CUSTOMER-VISIT-CONFIRMED", "交通", "structured_fact", {}, "POL-DEPT-SALES-TRANSPORT-2026-V1", "2026-01-01", None),
    ("ops-mkt-transport-limit@1.0", "RULE-OPS-MKT-TRANSPORT-LIMIT", "交通", "amount_limit", {"limit": "300.00"}, "POL-DEPT-OPS-MKT-2026-V1", "2026-01-01", None),
    ("ops-mkt-transport-receipt@1.0", "RULE-TRANSPORT-RECEIPT", "交通", "document_consistency", {}, "POL-DEPT-OPS-MKT-2026-V1", "2026-01-01", None),
    ("ops-mkt-transport-deadline@1.0", "RULE-SUBMISSION-DEADLINE", "交通", "timeliness", {"days": 45}, "POL-DEPT-OPS-MKT-2026-V1", "2026-01-01", None),
    ("ops-mkt-lodging-limit@1.0", "RULE-LODGING-AMOUNT-LIMIT", "住宿", "amount_limit", {"A": "680.00", "B": "520.00"}, "POL-DEPT-OPS-MKT-2026-V1", "2026-01-01", None),
    ("ops-mkt-lodging-submit@1.0", "RULE-SUBMISSION-TIMELINESS", "住宿", "timeliness", {"days": 45}, "POL-DEPT-OPS-MKT-2026-V1", "2026-01-01", None),
    ("ops-mkt-meal-limit@1.0", "RULE-OPS-MKT-MEAL-LIMIT", "餐饮", "amount_limit", {"per_person": "160.00"}, "POL-DEPT-OPS-MKT-2026-V1", "2026-01-01", None),
    ("lodging-nights@1.0", "RULE-LODGING-NIGHT-COUNT", "住宿", "calculation", {}, None, "2025-01-01", None),
    ("lodging-limit@1.0", "RULE-LODGING-AMOUNT-LIMIT", "住宿", "amount_limit", {"A": "600.00", "B": "450.00"}, "POL-LODGING-2026-V3", "2026-01-01", None),
    ("lodging-doc@1.0", "RULE-LODGING-DOCUMENT-CONSISTENCY", "住宿", "document_consistency", {}, None, "2025-01-01", None),
    ("lodging-submit@1.0", "RULE-SUBMISSION-TIMELINESS", "住宿", "timeliness", {"days": 30}, "POL-LODGING-2026-V3", "2026-01-01", None),
    ("duplicate@1.0", "RULE-DUPLICATE-INVOICE", None, "structured_fact", {}, None, "2025-01-01", None),
    ("budget@1.0", "RULE-BUDGET-AVAILABLE", None, "structured_fact", {}, None, "2025-01-01", None),
    ("permission@1.0", "RULE-APPLICANT-PERMISSION", None, "structured_fact", {}, None, "2025-01-01", None),
    ("meal-doc@1.0", "RULE-MEAL-DOCUMENT-CONSISTENCY", "餐饮", "document_consistency", {}, None, "2025-01-01", None),
    ("meal-version@1.0", "RULE-POLICY-VERSION-UNIQUENESS", "餐饮", "version_conflict", {}, None, "2025-01-01", None),
    ("meal-old@1.0", "RULE-MEAL-LIMIT-USING-OLD-POLICY", "餐饮", "amount_limit", {"per_person": "120.00"}, "POL-DINING-2025-V1", "2025-01-01", "2026-06-30"),
    ("meal-new@1.0", "RULE-MEAL-LIMIT-USING-NEW-POLICY", "餐饮", "amount_limit", {"per_person": "150.00"}, "POL-DINING-2026-V2", "2026-06-01", None),
    ("meal-determination@1.0", "RULE-MEAL-LIMIT-DETERMINATION", "餐饮", "conflict_resolution", {}, None, "2025-01-01", None),
]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def main() -> None:
    import os

    engine = create_engine(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    golden = json.loads((ROOT / "data/fixtures/golden_cases.json").read_text(encoding="utf-8"))
    with engine.begin() as connection:
        for case in golden["cases"]:
            request = case["input"]
            applicant = request["applicant"]
            application = request["application"]
            connection.execute(
                text(
                    """
                    INSERT INTO approval_requests (
                        request_id, thread_id, creation_idempotency_key,
                        employee_id, department_id, expense_type, currency,
                        amount, occurred_on, submitted_on, application
                    ) VALUES (
                        :request_id, :thread_id, :creation_idempotency_key,
                        :employee_id, :department_id, :expense_type, :currency,
                        :amount, :occurred_on, :submitted_on, CAST(:application AS jsonb)
                    ) ON CONFLICT (request_id) DO UPDATE SET
                        thread_id = EXCLUDED.thread_id,
                        application = EXCLUDED.application, updated_at = now()
                    """
                ),
                {
                    "request_id": request["request_id"],
                    "thread_id": f"approval:{request['request_id']}",
                    "creation_idempotency_key": (
                        f"fixture-create:{request['request_id']}"
                    ),
                    "employee_id": applicant["employee_id"],
                    "department_id": applicant["department_id"],
                    "expense_type": application["expense_type"],
                    "currency": application["currency"],
                    "amount": application["amount"],
                    "occurred_on": application["occurred_on"],
                    "submitted_on": application["submitted_on"],
                    "application": _json(application),
                },
            )
            for document in request["documents"]:
                connection.execute(
                    text(
                        """
                        INSERT INTO document_metadata (
                            document_id, request_id, document_type, media_type,
                            storage_uri, synthetic
                        ) VALUES (
                            :document_id, :request_id, :document_type, :media_type,
                            :storage_uri, :synthetic
                        ) ON CONFLICT (document_id) DO UPDATE SET
                            request_id = EXCLUDED.request_id,
                            document_type = EXCLUDED.document_type,
                            media_type = EXCLUDED.media_type
                        """
                    ),
                    {
                        **document,
                        "request_id": request["request_id"],
                        "storage_uri": f"fixture://{document['document_id']}",
                    },
                )
            for field in case["expected_extracted_fields"]:
                connection.execute(
                    text(
                        """
                        INSERT INTO extracted_fields (
                            request_id, field_name, status, value, raw_value, document_id, page
                        ) VALUES (
                            :request_id, :field_name, :status, CAST(:value AS jsonb),
                            :raw_value, :document_id, :page
                        ) ON CONFLICT ON CONSTRAINT uq_extracted_field_source DO UPDATE SET
                            status = EXCLUDED.status, value = EXCLUDED.value,
                            raw_value = EXCLUDED.raw_value, page = EXCLUDED.page
                        """
                    ),
                    {
                        "request_id": request["request_id"],
                        "field_name": field["field"],
                        "status": field["status"],
                        "value": _json(field.get("value")),
                        "raw_value": field.get("raw_value"),
                        "document_id": field.get("document_id"),
                        "page": field.get("page"),
                    },
                )
            connection.execute(
                text(
                    """
                    INSERT INTO audit_events (request_id, event_type, actor, payload, dedupe_key)
                    VALUES (:request_id, 'FIXTURE_SEEDED', 'SYSTEM', CAST(:payload AS jsonb), :key)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    """
                ),
                {
                    "request_id": request["request_id"],
                    "payload": _json({"case_id": case["case_id"]}),
                    "key": f"fixture:{case['case_id']}",
                },
            )

        for path in STRUCTURED_FIXTURES:
            fixture = json.loads(path.read_text(encoding="utf-8"))
            for record in fixture["records"]:
                connection.execute(
                    text(
                        """
                        INSERT INTO structured_records (
                            snapshot_id, fixture_id, query_type, record_key, snapshot_version,
                            effective_at, value, available_amount, consumer
                        ) VALUES (
                            :snapshot_id, :fixture_id, :query_type, :record_key,
                            :snapshot_version, :effective_at, CAST(:value AS jsonb),
                            :available_amount, :consumer
                        ) ON CONFLICT (snapshot_id, fixture_id) DO UPDATE SET
                            value = EXCLUDED.value, available_amount = EXCLUDED.available_amount,
                            snapshot_version = EXCLUDED.snapshot_version
                        """
                    ),
                    {
                        **record,
                        "snapshot_id": fixture["snapshot_id"],
                        "value": _json(record["value"]),
                        "available_amount": record.get("available_amount"),
                    },
                )

        for fixture in RULE_FIXTURES:
            (rule_key, rule_id, expense_type, rule_type, parameters, source, start, end) = fixture
            connection.execute(
                text(
                    """
                    INSERT INTO policy_rules (
                        id, rule_id, rule_version, expense_type, rule_type, parameters,
                        source_document_id, effective_from, effective_to
                    ) VALUES (
                        :id, :rule_id, '1.0', :expense_type, :rule_type,
                        CAST(:parameters AS jsonb), :source, :start, :end
                    ) ON CONFLICT (id) DO NOTHING
                    """
                ),
                {
                    "id": rule_key,
                    "rule_id": rule_id,
                    "expense_type": expense_type,
                    "rule_type": rule_type,
                    "parameters": _json(parameters),
                    "source": source,
                    "start": start,
                    "end": end,
                },
            )
    engine.dispose()
    print(
        f"seeded 3 approvals, "
        f"{sum(len(json.loads(path.read_text(encoding='utf-8'))['records']) for path in STRUCTURED_FIXTURES)} "
        f"structured records, and {len(RULE_FIXTURES)} policy rules"
    )


if __name__ == "__main__":
    main()
