"""PostgreSQL access for rules, durable approvals, and reviewed memory."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine

from app.models import (
    Applicant,
    Application,
    ApprovalStatus,
    DateOnly,
    Document,
    ExpenseType,
    ExtractedField,
    FinalDecision,
    HumanReviewSubmission,
    Recommendation,
    RiskLevel,
    StrictModel,
)
from app.rule_governance import replay_rule
from app.structured import DEMO_STRUCTURED_SNAPSHOT_ID, StructuredRecord

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/agentic_approval"
)


class IdempotencyConflictError(ValueError):
    pass


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def finalization_event_types(
    status: ApprovalStatus, recommendation: Recommendation | None
) -> tuple[str, ...]:
    if status not in {
        ApprovalStatus.COMPLETED,
        ApprovalStatus.BUSINESS_REJECTED,
        ApprovalStatus.INSUFFICIENT_EVIDENCE,
        ApprovalStatus.SYSTEM_ERROR,
    }:
        raise ValueError(f"cannot finalize non-terminal status: {status}")
    if status == ApprovalStatus.SYSTEM_ERROR:
        return ("APPROVAL_FINALIZED", "OPERATIONS_ALERT_REQUESTED")
    events = ["APPROVAL_FINALIZED", "NOTIFICATION_REQUESTED"]
    if recommendation in {
        Recommendation.PASS_RECOMMENDED,
        Recommendation.REJECT_RECOMMENDED,
    }:
        events.append("CASE_WRITE_REQUESTED")
    return tuple(events)


class PolicyRuleRecord(StrictModel):
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    expense_type: ExpenseType | None = None
    rule_type: str = Field(min_length=1)
    parameters: dict[str, Any]
    source_document_id: str | None = None
    effective_from: DateOnly
    effective_to: DateOnly | None = None


class PostgresStore:
    def __init__(
        self, database_url: str | None = None, *, connect_timeout: int | None = None
    ) -> None:
        self.engine: Engine = create_engine(
            database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
            pool_pre_ping=True,
            connect_args={"connect_timeout": connect_timeout} if connect_timeout is not None else {},
        )

    def close(self) -> None:
        self.engine.dispose()

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def schema_ready(self) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        """SELECT
                            to_regclass('alembic_version') IS NOT NULL
                            AND to_regclass('approval_requests') IS NOT NULL
                            AND to_regclass('document_metadata') IS NOT NULL
                            AND to_regclass('extracted_fields') IS NOT NULL
                            AND to_regclass('policy_rules') IS NOT NULL
                            AND to_regclass('structured_records') IS NOT NULL
                            AND to_regclass('audit_events') IS NOT NULL
                            AND to_regclass('approval_cases') IS NOT NULL
                            AND to_regclass('candidate_rules') IS NOT NULL"""
                    )
                ).scalar_one()
            )

    @contextmanager
    def request_lock(self, request_id: str) -> Iterator[bool]:
        with self.engine.connect() as connection:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:request_id, 0))"),
                    {"request_id": request_id},
                ).scalar_one()
            )
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(hashtextextended(:request_id, 0))"),
                        {"request_id": request_id},
                    )

    def create_approval(
        self,
        *,
        request_id: str,
        thread_id: str,
        idempotency_key: str,
        applicant: Applicant,
        application: Application,
        documents: list[Document],
    ) -> bool:
        application_payload = application.model_dump(mode="json")
        applicant_payload = applicant.model_dump(mode="json")
        document_payload = [document.model_dump(mode="json") for document in documents]
        with self.engine.begin() as connection:
            inserted = connection.execute(
                text(
                    """
                    INSERT INTO approval_requests (
                        request_id, thread_id, creation_idempotency_key,
                        employee_id, department_id, expense_type, currency,
                        amount, occurred_on, submitted_on, application, status
                    ) VALUES (
                        :request_id, :thread_id, :idempotency_key,
                        :employee_id, :department_id, :expense_type, :currency,
                        :amount, :occurred_on, :submitted_on,
                        CAST(:application AS jsonb), 'RUNNING'
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING request_id
                    """
                ),
                {
                    "request_id": request_id,
                    "thread_id": thread_id,
                    "idempotency_key": idempotency_key,
                    "employee_id": applicant.employee_id,
                    "department_id": applicant.department_id,
                    "expense_type": application.expense_type.value,
                    "currency": application.currency,
                    "amount": application.amount,
                    "occurred_on": application.occurred_on,
                    "submitted_on": application.submitted_on,
                    "application": _json(application_payload),
                },
            ).scalar_one_or_none()
            if inserted is None:
                # A replay must reuse both the request ID and its original key.
                matches = connection.execute(
                    text(
                        """
                        SELECT request_id, thread_id, creation_idempotency_key,
                               application, employee_id, department_id
                        FROM approval_requests
                        WHERE creation_idempotency_key = :idempotency_key
                           OR request_id = :request_id
                        """
                    ),
                    {
                        "idempotency_key": idempotency_key,
                        "request_id": request_id,
                    },
                ).mappings().all()
                if len(matches) != 1:
                    raise IdempotencyConflictError(
                        "idempotency key and request ID belong to different approvals"
                    )
                existing = matches[0]
                if (
                    existing["request_id"] != request_id
                    or existing["thread_id"] != thread_id
                    or existing["creation_idempotency_key"] != idempotency_key
                    or existing["application"] != application_payload
                    or existing["employee_id"] != applicant.employee_id
                    or existing["department_id"] != applicant.department_id
                ):
                    raise IdempotencyConflictError(
                        "idempotency key or request ID was already used for another payload"
                    )
                created_payload = connection.execute(
                    text(
                        "SELECT payload FROM audit_events WHERE request_id = :request_id "
                        "AND event_type = 'APPROVAL_CREATED' ORDER BY event_id LIMIT 1"
                    ),
                    {"request_id": request_id},
                ).scalar_one_or_none()
                if created_payload and "applicant" in created_payload:
                    if (
                        created_payload["applicant"] != applicant_payload
                        or created_payload["documents"] != document_payload
                    ):
                        raise IdempotencyConflictError(
                            "request ID was reused with another applicant or document list"
                        )
                else:
                    # Older records have no creation snapshot; compare their current metadata.
                    existing_documents = connection.execute(
                        text(
                            "SELECT document_id, document_type, media_type, synthetic "
                            "FROM document_metadata WHERE request_id = :request_id "
                            "ORDER BY document_id"
                        ),
                        {"request_id": request_id},
                    ).mappings().all()
                    incoming_documents = sorted(
                        (
                            {
                                "document_id": item.document_id,
                                "document_type": item.document_type,
                                "media_type": item.media_type,
                                "synthetic": item.synthetic,
                            }
                            for item in documents
                        ),
                        key=lambda item: item["document_id"],
                    )
                    if [dict(item) for item in existing_documents] != incoming_documents:
                        raise IdempotencyConflictError(
                            "request ID was reused with another document list"
                        )
                return False

            for document in documents:
                saved_document = connection.execute(
                    text(
                        """
                        INSERT INTO document_metadata (
                            document_id, request_id, document_type, media_type, synthetic
                        ) VALUES (
                            :document_id, :request_id, :document_type, :media_type, :synthetic
                        ) ON CONFLICT (document_id) DO NOTHING
                        RETURNING document_id
                        """
                    ),
                    {"request_id": request_id, **document.model_dump(mode="python")},
                ).scalar_one_or_none()
                if saved_document is None:
                    raise IdempotencyConflictError(
                        f"document ID {document.document_id!r} is already in use"
                    )
            self._insert_event(
                connection,
                request_id=request_id,
                event_type="APPROVAL_CREATED",
                actor="SYSTEM",
                payload={
                    "thread_id": thread_id,
                    "applicant": applicant_payload,
                    "documents": document_payload,
                },
                dedupe_key=f"create:{idempotency_key}",
            )
        return True

    @staticmethod
    def _insert_event(
        connection: Any,
        *,
        request_id: str | None,
        event_type: str,
        actor: str,
        payload: dict[str, object],
        dedupe_key: str,
    ) -> bool:
        inserted = connection.execute(
            text(
                """
                INSERT INTO audit_events (
                    request_id, event_type, actor, payload, dedupe_key
                ) VALUES (
                    :request_id, :event_type, :actor, CAST(:payload AS jsonb), :dedupe_key
                )
                ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING event_id
                """
            ),
            {
                "request_id": request_id,
                "event_type": event_type,
                "actor": actor,
                "payload": _json(payload),
                "dedupe_key": dedupe_key,
            },
        ).scalar_one_or_none()
        if inserted is not None:
            return True
        existing = connection.execute(
            text("SELECT payload FROM audit_events WHERE dedupe_key = :key"),
            {"key": dedupe_key},
        ).scalar_one()
        if existing != payload:
            raise IdempotencyConflictError(
                f"idempotency key {dedupe_key!r} was reused with another payload"
            )
        return False

    def finalize_approval(
        self,
        *,
        request_id: str,
        status: ApprovalStatus | str,
        recommendation: Recommendation | str | None,
        risk_level: RiskLevel | str | None,
        final_decision: FinalDecision | dict[str, object] | None,
        human_idempotency_key: str | None,
    ) -> None:
        status = ApprovalStatus(status)
        recommendation = Recommendation(recommendation) if recommendation else None
        risk_level = RiskLevel(risk_level) if risk_level else None
        final_decision = (
            FinalDecision.model_validate(final_decision) if final_decision else None
        )
        decision_payload = (
            final_decision.model_dump(mode="json") if final_decision else None
        )
        result_payload: dict[str, object] = {
            "status": status.value,
            "recommendation": recommendation.value if recommendation else None,
            "risk_level": risk_level.value if risk_level else None,
            "final_decision": decision_payload,
        }
        actor = final_decision.operator_id if final_decision else "SYSTEM"
        with self.engine.begin() as connection:
            if final_decision and human_idempotency_key:
                self._insert_event(
                    connection,
                    request_id=request_id,
                    event_type="HUMAN_REVIEWED",
                    actor=actor,
                    payload={
                        "action": final_decision.action.value,
                        "before": (
                            final_decision.previous_recommendation.value
                            if final_decision.previous_recommendation
                            else None
                        ),
                        "after": final_decision.recommendation.value,
                        "reason": final_decision.reason,
                    },
                    dedupe_key=f"human:{request_id}:{human_idempotency_key}",
                )
            updated = connection.execute(
                text(
                    """
                    UPDATE approval_requests
                    SET status = :status,
                        recommendation = :recommendation,
                        risk_level = :risk_level,
                        final_decision = CAST(:final_decision AS jsonb),
                        updated_at = now()
                    WHERE request_id = :request_id
                    """
                ),
                {
                    "request_id": request_id,
                    "status": status.value,
                    "recommendation": recommendation.value if recommendation else None,
                    "risk_level": risk_level.value if risk_level else None,
                    "final_decision": _json(decision_payload),
                },
            )
            if updated.rowcount != 1:
                raise LookupError(f"unknown approval request: {request_id}")
            key_prefixes = {
                "APPROVAL_FINALIZED": "finalize",
                "NOTIFICATION_REQUESTED": "notification",
                "CASE_WRITE_REQUESTED": "case-write",
                "OPERATIONS_ALERT_REQUESTED": "operations-alert",
            }
            for event_type in finalization_event_types(status, recommendation):
                self._insert_event(
                    connection,
                    request_id=request_id,
                    event_type=event_type,
                    actor=actor,
                    payload=result_payload,
                    dedupe_key=f"{key_prefixes[event_type]}:{request_id}",
                )

    def mark_human_pending(
        self,
        *,
        request_id: str,
        status: ApprovalStatus | str,
        recommendation: Recommendation | str,
        risk_level: RiskLevel | str | None,
        missing_items: list[str],
        allowed_actions: list[str],
    ) -> None:
        status = ApprovalStatus(status)
        recommendation = Recommendation(recommendation)
        risk_level = RiskLevel(risk_level) if risk_level else None
        payload: dict[str, object] = {
            "status": status.value,
            "recommendation": recommendation.value,
            "risk_level": risk_level.value if risk_level else None,
            "missing_items": missing_items,
            "allowed_actions": allowed_actions,
        }
        with self.engine.begin() as connection:
            updated = connection.execute(
                text(
                    """
                    UPDATE approval_requests
                    SET status = :status,
                        recommendation = :recommendation,
                        risk_level = :risk_level,
                        updated_at = now()
                    WHERE request_id = :request_id
                    """
                ),
                {
                    "request_id": request_id,
                    "status": status.value,
                    "recommendation": recommendation.value,
                    "risk_level": risk_level.value if risk_level else None,
                },
            )
            if updated.rowcount != 1:
                raise LookupError(f"unknown approval request: {request_id}")
            self._insert_event(
                connection,
                request_id=request_id,
                event_type="HUMAN_REVIEW_REQUESTED",
                actor="SYSTEM",
                payload=payload,
                dedupe_key=f"human-pending:{request_id}",
            )

    def approval_record(
        self,
        request_id: str,
        *,
        department_ids: list[str] | None = None,
        employee_id: str | None = None,
    ) -> dict[str, object] | None:
        conditions = ["request_id = :request_id"]
        parameters: dict[str, object] = {"request_id": request_id}
        if department_ids is not None:
            conditions.append("department_id IN :department_ids")
            parameters["department_ids"] = department_ids
        if employee_id is not None:
            conditions.append("employee_id = :employee_id")
            parameters["employee_id"] = employee_id
        statement = text(
            "SELECT request_id, thread_id, employee_id, department_id, status, "
            "recommendation, risk_level, final_decision, updated_at FROM approval_requests "
            f"WHERE {' AND '.join(conditions)}"
        )
        if department_ids is not None:
            statement = statement.bindparams(bindparam("department_ids", expanding=True))
        with self.engine.connect() as connection:
            row = connection.execute(statement, parameters).mappings().first()
        return dict(row) if row else None

    def audit_event_counts(self, request_id: str) -> dict[str, int]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT event_type, count(*) AS count
                    FROM audit_events
                    WHERE request_id = :request_id
                    GROUP BY event_type
                    """
                ),
                {"request_id": request_id},
            ).mappings()
            return {row["event_type"]: row["count"] for row in rows}

    def record_event(
        self,
        *,
        request_id: str | None,
        event_type: str,
        actor: str,
        payload: dict[str, object],
        dedupe_key: str,
    ) -> None:
        with self.engine.begin() as connection:
            self._insert_event(
                connection,
                request_id=request_id,
                event_type=event_type,
                actor=actor,
                payload=payload,
                dedupe_key=dedupe_key,
            )

    def claim_human_review(
        self,
        request_id: str,
        submission: HumanReviewSubmission,
        *,
        expected_status: str | None,
    ) -> tuple[bool, str]:
        payload = submission.model_dump(mode="json", exclude_none=True)
        dedupe_key = f"human-review-claim:{request_id}"
        with self.engine.begin() as connection:
            status = connection.execute(
                text("SELECT status FROM approval_requests WHERE request_id = :id FOR UPDATE"),
                {"id": request_id},
            ).scalar_one_or_none()
            if status is None:
                raise LookupError(f"unknown approval request: {request_id}")
            existing = connection.execute(
                text("SELECT payload FROM audit_events WHERE dedupe_key = :key"),
                {"key": dedupe_key},
            ).scalar_one_or_none()
            if existing is not None:
                if existing != payload:
                    raise IdempotencyConflictError("另一份人工复核已被受理")
                return False, str(status)
            if expected_status is None or status != expected_status:
                raise ValueError("当前审批不接受新的人工复核")
            self._insert_event(
                connection,
                request_id=request_id,
                event_type="HUMAN_REVIEW_ACCEPTED",
                actor=submission.operator_id,
                payload=payload,
                dedupe_key=dedupe_key,
            )
            return True, str(status)

    def accepted_human_review(self, request_id: str) -> HumanReviewSubmission | None:
        with self.engine.connect() as connection:
            payload = connection.execute(
                text("SELECT payload FROM audit_events WHERE dedupe_key = :key"),
                {"key": f"human-review-claim:{request_id}"},
            ).scalar_one_or_none()
        return HumanReviewSubmission.model_validate(payload) if payload else None

    def mark_workflow_failed(
        self,
        request_id: str,
        *,
        error: str,
        message: str,
        failure_key: str,
        trace_id: str,
    ) -> bool:
        with self.engine.begin() as connection:
            updated = connection.execute(
                text(
                    """
                    UPDATE approval_requests
                    SET status = 'SYSTEM_ERROR',
                        recommendation = NULL,
                        risk_level = NULL,
                        updated_at = now()
                    WHERE request_id = :request_id AND status = 'RUNNING'
                    """
                ),
                {"request_id": request_id},
            )
            if updated.rowcount != 1:
                return False
            retry_count = connection.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE request_id = :request_id "
                    "AND event_type = 'WORKFLOW_RETRY_REQUESTED'"
                ),
                {"request_id": request_id},
            ).scalar_one()
            payload = {"trace_id": trace_id, "error": error, "message": message}
            self._insert_event(
                connection,
                request_id=request_id,
                event_type="WORKFLOW_FAILED",
                actor="SYSTEM",
                payload=payload,
                dedupe_key=f"workflow-failed:{request_id}:{retry_count}:{failure_key}",
            )
            self._insert_event(
                connection,
                request_id=request_id,
                event_type="OPERATIONS_ALERT_REQUESTED",
                actor="SYSTEM",
                payload=payload,
                dedupe_key=f"workflow-failed-alert:{request_id}:{retry_count}:{failure_key}",
            )
            return True

    def mark_workflow_retrying(
        self,
        request_id: str,
        *,
        actor: str,
        idempotency_key: str,
        expected_status: str = "SYSTEM_ERROR",
        stale_before: datetime | None = None,
    ) -> bool:
        with self.engine.begin() as connection:
            retry_key = f"workflow-retry:{request_id}:{idempotency_key}"
            if connection.execute(
                text("SELECT 1 FROM audit_events WHERE dedupe_key = :dedupe_key"),
                {"dedupe_key": retry_key},
            ).scalar_one_or_none():
                return False
            stale_condition = " AND updated_at < :stale_before" if stale_before else ""
            updated = connection.execute(
                text(
                    f"""
                    UPDATE approval_requests
                    SET status = 'RUNNING', recommendation = NULL,
                        risk_level = NULL, updated_at = now()
                    WHERE request_id = :request_id AND status = :expected_status{stale_condition}
                    """
                ),
                {
                    "request_id": request_id,
                    "expected_status": expected_status,
                    **({"stale_before": stale_before} if stale_before else {}),
                },
            )
            if updated.rowcount != 1:
                return False
            self._insert_event(
                connection,
                request_id=request_id,
                event_type="WORKFLOW_RETRY_REQUESTED",
                actor=actor,
                payload={},
                dedupe_key=retry_key,
            )
            return True

    def list_approvals(
        self,
        *,
        page: int,
        page_size: int,
        status: ApprovalStatus | None = None,
        human_only: bool = False,
        department_ids: list[str] | None = None,
        employee_id: str | None = None,
    ) -> tuple[list[dict[str, object]], int]:
        conditions = []
        parameters: dict[str, object] = {
            "limit": page_size,
            "offset": (page - 1) * page_size,
        }
        if status is not None:
            conditions.append("status = :status")
            parameters["status"] = status.value
        if human_only:
            conditions.append("status IN ('HUMAN_PENDING', 'INSUFFICIENT_EVIDENCE')")
        if department_ids is not None:
            conditions.append("department_id IN :department_ids")
            parameters["department_ids"] = department_ids
        if employee_id is not None:
            conditions.append("employee_id = :employee_id")
            parameters["employee_id"] = employee_id
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        count_query = text(f"SELECT count(*) FROM approval_requests {where}")
        list_query = text(
            f"""
            SELECT request_id, employee_id, department_id, expense_type,
                   currency, amount, occurred_on, submitted_on, status,
                   recommendation, risk_level, created_at, updated_at
            FROM approval_requests
            {where}
            ORDER BY updated_at DESC, request_id
            LIMIT :limit OFFSET :offset
            """
        )
        if department_ids is not None:
            expanding = bindparam("department_ids", expanding=True)
            count_query = count_query.bindparams(expanding)
            list_query = list_query.bindparams(bindparam("department_ids", expanding=True))
        with self.engine.connect() as connection:
            total = connection.execute(count_query, parameters).scalar_one()
            rows = connection.execute(list_query, parameters).mappings()
            return [dict(row) for row in rows], total

    def approval_detail(self, request_id: str) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                    SELECT request_id, thread_id, employee_id, department_id,
                           expense_type, currency, amount, occurred_on, submitted_on,
                           application, status, recommendation, risk_level,
                           final_decision, created_at, updated_at
                    FROM approval_requests
                    WHERE request_id = :request_id
                    """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            documents = connection.execute(
                text(
                    """
                    SELECT document_id, document_type, media_type, storage_uri,
                           sha256, synthetic, created_at
                    FROM document_metadata
                    WHERE request_id = :request_id
                    ORDER BY created_at, document_id
                    """
                ),
                {"request_id": request_id},
            ).mappings()
            return {**dict(row), "documents": [dict(item) for item in documents]}

    def audit_events_after(
        self,
        request_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT event_id, request_id, event_type, actor, payload, created_at
                    FROM audit_events
                    WHERE request_id = :request_id AND event_id > :after_event_id
                    ORDER BY event_id
                    LIMIT :limit
                    """
                ),
                {
                    "request_id": request_id,
                    "after_event_id": after_event_id,
                    "limit": limit,
                },
            ).mappings()
            return [dict(row) for row in rows]

    def save_document(
        self,
        *,
        request_id: str,
        document_id: str,
        document_type: str,
        media_type: str,
        storage_uri: str,
        sha256: str,
    ) -> None:
        with self.engine.begin() as connection:
            if (
                connection.execute(
                    text("SELECT 1 FROM approval_requests WHERE request_id = :request_id"),
                    {"request_id": request_id},
                ).scalar_one_or_none()
                is None
            ):
                raise LookupError(f"unknown approval request: {request_id}")
            saved = connection.execute(
                text(
                    """
                    INSERT INTO document_metadata (
                        document_id, request_id, document_type, media_type,
                        storage_uri, sha256, synthetic
                    ) VALUES (
                        :document_id, :request_id, :document_type, :media_type,
                        :storage_uri, :sha256, false
                    ) ON CONFLICT (document_id) DO UPDATE SET
                        document_type = EXCLUDED.document_type,
                        media_type = EXCLUDED.media_type,
                        storage_uri = EXCLUDED.storage_uri,
                        sha256 = EXCLUDED.sha256
                    WHERE document_metadata.request_id = EXCLUDED.request_id
                      AND document_metadata.synthetic = false
                      AND (
                          document_metadata.sha256 IS NULL
                          OR (
                              document_metadata.sha256 = EXCLUDED.sha256
                              AND document_metadata.document_type = EXCLUDED.document_type
                              AND document_metadata.media_type = EXCLUDED.media_type
                              AND document_metadata.storage_uri = EXCLUDED.storage_uri
                          )
                      )
                    RETURNING document_id
                    """
                ),
                {
                    "document_id": document_id,
                    "request_id": request_id,
                    "document_type": document_type,
                    "media_type": media_type,
                    "storage_uri": storage_uri,
                    "sha256": sha256,
                },
            ).scalar_one_or_none()
            if saved is None:
                raise IdempotencyConflictError(
                    f"document ID {document_id!r} already has a stored file"
                )

    def upsert_extracted_fields(self, request_id: str, fields: list[ExtractedField]) -> None:
        with self.engine.begin() as connection:
            for field in fields:
                connection.execute(
                    text(
                        """
                        INSERT INTO extracted_fields (
                            request_id, field_name, status, value, raw_value,
                            document_id, page
                        ) VALUES (
                            :request_id, :field_name, :status, CAST(:value AS jsonb),
                            :raw_value, :document_id, :page
                        ) ON CONFLICT ON CONSTRAINT uq_extracted_field_source DO UPDATE SET
                            status = EXCLUDED.status,
                            value = EXCLUDED.value,
                            raw_value = EXCLUDED.raw_value,
                            page = EXCLUDED.page
                        """
                    ),
                    {
                        "request_id": request_id,
                        "field_name": field.field,
                        "status": field.status.value,
                        "value": _json(field.value),
                        "raw_value": field.raw_value,
                        "document_id": field.document_id,
                        "page": field.page,
                    },
                )

    def policy_upload_events(self) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT event_id, payload, created_at
                    FROM audit_events
                    WHERE event_type = 'POLICY_UPLOADED'
                    ORDER BY event_id DESC
                    """
                )
            ).mappings()
            return [dict(row) for row in rows]

    def find(
        self,
        *,
        query_type: str,
        record_key: str,
        snapshot_ids: set[str],
        as_of: DateOnly,
        request_id: str | None = None,
    ) -> tuple[str, StructuredRecord] | None:
        if not snapshot_ids:
            return None
        if (
            query_type == "duplicate_invoice"
            and DEMO_STRUCTURED_SNAPSHOT_ID in snapshot_ids
            and request_id is not None
        ):
            return self._live_duplicate_invoice(request_id, record_key, as_of)
        statement = text(
            """
            SELECT snapshot_id, fixture_id, query_type, record_key, snapshot_version,
                   effective_at, value, available_amount, consumer
            FROM structured_records
            WHERE snapshot_id IN :snapshot_ids
              AND query_type = :query_type
              AND record_key = :record_key
              AND effective_at <= :as_of
            ORDER BY effective_at DESC, snapshot_version DESC
            LIMIT 1
            """
        ).bindparams(bindparam("snapshot_ids", expanding=True))
        with self.engine.connect() as connection:
            row = connection.execute(
                statement,
                {
                    "snapshot_ids": sorted(snapshot_ids),
                    "query_type": query_type,
                    "record_key": record_key,
                    "as_of": as_of,
                },
            ).mappings().first()
        if row is None:
            return None
        return row["snapshot_id"], StructuredRecord.model_validate(
            {key: row[key] for key in StructuredRecord.model_fields}
        )

    def _live_duplicate_invoice(
        self, request_id: str, invoice_number: str, as_of: DateOnly
    ) -> tuple[str, StructuredRecord] | None:
        # The demo snapshot's false value is only a fixture. Real uploads are checked
        # against earlier uploaded applications, excluding preloaded fixture:// files.
        with self.engine.connect() as connection:
            duplicate = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM approval_requests previous
                        JOIN extracted_fields field ON field.request_id = previous.request_id
                        JOIN document_metadata document
                          ON document.document_id = field.document_id
                         AND document.request_id = previous.request_id
                        WHERE previous.request_id <> current_request.request_id
                          AND previous.created_at < current_request.created_at
                          AND field.field_name = 'invoice_number'
                          AND field.status = 'PRESENT'
                          AND field.value #>> '{}' = :invoice_number
                          AND document.storage_uri IS NOT NULL
                          AND document.storage_uri NOT LIKE 'fixture://%'
                    ) AS duplicate
                    FROM approval_requests current_request
                    JOIN extracted_fields current_field
                      ON current_field.request_id = current_request.request_id
                    JOIN document_metadata current_document
                      ON current_document.document_id = current_field.document_id
                     AND current_document.request_id = current_request.request_id
                    WHERE current_request.request_id = :request_id
                      AND current_field.field_name = 'invoice_number'
                      AND current_field.status = 'PRESENT'
                      AND current_field.value #>> '{}' = :invoice_number
                      AND current_document.storage_uri IS NOT NULL
                      AND current_document.storage_uri NOT LIKE 'fixture://%'
                    LIMIT 1
                    """
                ),
                {"request_id": request_id, "invoice_number": invoice_number},
            ).scalar_one_or_none()
        if duplicate is None:
            return None
        return DEMO_STRUCTURED_SNAPSHOT_ID, StructuredRecord(
            fixture_id=f"LIVE-INVOICE-{request_id}",
            query_type="duplicate_invoice",
            record_key=invoice_number,
            snapshot_version="LIVE-UPLOADED-APPLICATIONS",
            effective_at=as_of,
            value=bool(duplicate),
            consumer="RULE_VALIDATOR",
        )

    def extracted_fields(self, request_id: str) -> list[ExtractedField]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT field_name, status, value, raw_value, document_id, page
                    FROM extracted_fields
                    WHERE request_id = :request_id
                    ORDER BY id
                    """
                ),
                {"request_id": request_id},
            ).mappings()
            return [
                ExtractedField(
                    field=row["field_name"],
                    status=row["status"],
                    value=row["value"],
                    raw_value=row["raw_value"],
                    document_id=row["document_id"],
                    page=row["page"],
                )
                for row in rows
            ]

    def policy_rules(
        self,
        *,
        expense_type: ExpenseType,
        effective_at: date,
        document_ids: set[str],
    ) -> list[PolicyRuleRecord]:
        document_clause = (
            "AND (source_document_id IS NULL OR source_document_id IN :document_ids)"
            if document_ids
            else "AND source_document_id IS NULL"
        )
        statement = text(
            f"""
            SELECT rule_id, rule_version, expense_type, rule_type, parameters,
                   source_document_id, effective_from, effective_to
            FROM policy_rules
            WHERE enabled = true
              AND (expense_type IS NULL OR expense_type = :expense_type)
              AND effective_from <= :effective_at
              AND (effective_to IS NULL OR effective_to >= :effective_at)
              {document_clause}
            ORDER BY rule_id, source_document_id NULLS FIRST
            """
        )
        parameters: dict[str, object] = {
            "expense_type": expense_type.value,
            "effective_at": effective_at,
        }
        if document_ids:
            statement = statement.bindparams(bindparam("document_ids", expanding=True))
            parameters["document_ids"] = sorted(document_ids)
        with self.engine.connect() as connection:
            rows = connection.execute(statement, parameters).mappings()
            return [PolicyRuleRecord.model_validate(dict(row)) for row in rows]

    def case_write_requested(self, request_id: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        "SELECT 1 FROM audit_events WHERE request_id = :request_id "
                        "AND event_type = 'CASE_WRITE_REQUESTED'"
                    ),
                    {"request_id": request_id},
                ).scalar_one_or_none()
            )

    def pending_case_requests(self) -> list[str]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    text(
                        """
                        SELECT DISTINCT e.request_id FROM audit_events e
                        LEFT JOIN approval_cases c ON c.source_request_id = e.request_id
                        WHERE e.event_type = 'CASE_WRITE_REQUESTED'
                          AND c.case_id IS NULL
                        ORDER BY e.request_id
                        """
                    )
                ).scalars()
            )

    def case_exists(self, request_id: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text("SELECT 1 FROM approval_cases WHERE source_request_id = :request_id"),
                    {"request_id": request_id},
                ).scalar_one_or_none()
            )

    def insert_case(
        self,
        *,
        source_request_id: str,
        department_id: str,
        expense_type: str,
        occurred_on: date,
        summary: str,
        recommendation: str,
        human_action: str | None,
        rule_refs: list[dict[str, object]],
        evidence_refs: list[dict[str, object]],
        embedding: list[float] | None,
    ) -> bool:
        with self.engine.begin() as connection:
            inserted = connection.execute(
                text(
                    """
                    INSERT INTO approval_cases (
                        case_id, source_request_id, department_id, expense_type,
                        occurred_on, summary, recommendation, human_action,
                        rule_refs, evidence_refs, embedding
                    ) VALUES (
                        :case_id, :source_request_id, :department_id, :expense_type,
                        :occurred_on, :summary, :recommendation, :human_action,
                        CAST(:rule_refs AS jsonb), CAST(:evidence_refs AS jsonb),
                        CAST(:embedding AS jsonb)
                    ) ON CONFLICT (source_request_id) DO NOTHING
                    RETURNING case_id
                    """
                ),
                {
                    "case_id": f"CASE-{uuid4().hex}",
                    "source_request_id": source_request_id,
                    "department_id": department_id,
                    "expense_type": expense_type,
                    "occurred_on": occurred_on,
                    "summary": summary,
                    "recommendation": recommendation,
                    "human_action": human_action,
                    "rule_refs": _json(rule_refs),
                    "evidence_refs": _json(evidence_refs),
                    "embedding": _json(embedding),
                },
            ).scalar_one_or_none()
            if inserted is None:
                return False
            self._insert_event(
                connection,
                request_id=source_request_id,
                event_type="CASE_WRITTEN",
                actor="SYSTEM",
                payload={"case_id": inserted},
                dedupe_key=f"case-written:{source_request_id}",
            )
            return True

    def case_candidates(
        self, *, department_id: str, expense_type: str, effective_at: date
    ) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT case_id, summary, recommendation, human_action,
                           rule_refs, evidence_refs, embedding, occurred_on
                    FROM approval_cases
                    WHERE department_id = :department_id
                      AND expense_type = :expense_type
                      AND occurred_on <= :effective_at
                    ORDER BY created_at DESC LIMIT 100
                    """
                ),
                {
                    "department_id": department_id,
                    "expense_type": expense_type,
                    "effective_at": effective_at,
                },
            ).mappings()
            return [dict(row) for row in rows]

    def proposable_rule_ids(self, rule_ids: list[str]) -> list[str]:
        if not rule_ids:
            return []
        statement = text(
            """
            SELECT DISTINCT rule_id FROM policy_rules
            WHERE rule_id IN :rule_ids AND enabled = true
              AND rule_type IN ('amount_limit', 'timeliness')
            """
        ).bindparams(bindparam("rule_ids", expanding=True))
        with self.engine.connect() as connection:
            return list(connection.execute(statement, {"rule_ids": rule_ids}).scalars())

    def candidate_exists(self, request_id: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text("SELECT 1 FROM candidate_rules WHERE source_request_id = :request_id"),
                    {"request_id": request_id},
                ).scalar_one_or_none()
            )

    def insert_candidate(
        self,
        *,
        source_request_id: str,
        target_rule_id: str,
        department_id: str,
        expense_type: str,
        summary: str,
        parameters: dict[str, str | int],
    ) -> bool:
        with self.engine.begin() as connection:
            candidate_id = connection.execute(
                text(
                    """
                    INSERT INTO candidate_rules (
                        source_request_id, target_rule_id, department_id,
                        expense_type, summary, parameters
                    ) VALUES (
                        :source_request_id, :target_rule_id, :department_id,
                        :expense_type, :summary, CAST(:parameters AS jsonb)
                    ) ON CONFLICT (source_request_id) DO NOTHING
                    RETURNING candidate_id
                    """
                ),
                {
                    "source_request_id": source_request_id,
                    "target_rule_id": target_rule_id,
                    "department_id": department_id,
                    "expense_type": expense_type,
                    "summary": summary,
                    "parameters": _json(parameters),
                },
            ).scalar_one_or_none()
            if candidate_id is None:
                return False
            self._insert_event(
                connection,
                request_id=source_request_id,
                event_type="RULE_CANDIDATE_CREATED",
                actor="SYSTEM",
                payload={"candidate_id": candidate_id, "target_rule_id": target_rule_id},
                dedupe_key=f"candidate-created:{source_request_id}",
            )
            return True

    def rule_catalog(self) -> dict[str, list[dict[str, object]]]:
        with self.engine.connect() as connection:
            rules = connection.execute(
                text(
                    """
                    SELECT id, rule_id, rule_version, expense_type, rule_type,
                           parameters, source_document_id, effective_from,
                           effective_to, enabled
                    FROM policy_rules ORDER BY rule_id, effective_from DESC
                    """
                )
            ).mappings()
            candidates = connection.execute(
                text(
                    """
                    SELECT candidate_id, target_rule_id, department_id,
                           expense_type, summary, parameters, status,
                           reviewer_id, source_document_id, effective_from,
                           replay_cases, published_rule_row_id, published_at,
                           reviewed_at, created_at
                    FROM candidate_rules ORDER BY candidate_id DESC
                    """
                )
            ).mappings()
            return {
                "rules": [dict(item) for item in rules],
                "candidates": [dict(item) for item in candidates],
            }

    def approve_candidate(
        self,
        candidate_id: int,
        *,
        reviewer_id: str,
        source_document_id: str,
        effective_from: date,
        parameters: dict[str, object],
        replay_cases: list[dict[str, object]],
    ) -> None:
        if effective_from <= datetime.now(UTC).date():
            raise ValueError("candidate effective date must be in the future")
        with self.engine.begin() as connection:
            candidate = connection.execute(
                text("SELECT * FROM candidate_rules WHERE candidate_id = :id FOR UPDATE"),
                {"id": candidate_id},
            ).mappings().first()
            if candidate is None:
                raise LookupError(f"unknown candidate: {candidate_id}")
            if candidate["status"] != "PENDING":
                raise IdempotencyConflictError("only pending candidates can be approved")
            base = connection.execute(
                text(
                    """
                    SELECT * FROM policy_rules
                    WHERE rule_id = :rule_id AND source_document_id = :source
                      AND enabled = true AND effective_from < :effective_from
                      AND (effective_to IS NULL OR effective_to >= :effective_from)
                    ORDER BY effective_from DESC LIMIT 1
                    """
                ),
                {
                    "rule_id": candidate["target_rule_id"],
                    "source": source_document_id,
                    "effective_from": effective_from,
                },
            ).mappings().first()
            if base is None or base["expense_type"] != candidate["expense_type"]:
                raise ValueError("candidate must revise one applicable published rule")
            try:
                replay_rule(base["rule_type"], base["parameters"], parameters, replay_cases)
            except (KeyError, ArithmeticError) as exc:
                raise ValueError("invalid rule replay input") from exc
            connection.execute(
                text(
                    """
                    UPDATE candidate_rules SET status = 'APPROVED',
                        reviewer_id = :reviewer, source_document_id = :source,
                        effective_from = :effective_from,
                        parameters = CAST(:parameters AS jsonb),
                        replay_cases = CAST(:replay_cases AS jsonb), reviewed_at = now()
                    WHERE candidate_id = :id
                    """
                ),
                {
                    "id": candidate_id,
                    "reviewer": reviewer_id,
                    "source": source_document_id,
                    "effective_from": effective_from,
                    "parameters": _json(parameters),
                    "replay_cases": _json(replay_cases),
                },
            )
            self._insert_event(
                connection,
                request_id=candidate["source_request_id"],
                event_type="RULE_CANDIDATE_APPROVED",
                actor=reviewer_id,
                payload={"candidate_id": candidate_id, "effective_from": effective_from.isoformat()},
                dedupe_key=f"candidate-approved:{candidate_id}",
            )

    def reject_candidate(self, candidate_id: int, *, reviewer_id: str) -> None:
        with self.engine.begin() as connection:
            candidate = connection.execute(
                text("SELECT * FROM candidate_rules WHERE candidate_id = :id FOR UPDATE"),
                {"id": candidate_id},
            ).mappings().first()
            if candidate is None:
                raise LookupError(f"unknown candidate: {candidate_id}")
            if candidate["status"] != "PENDING":
                raise IdempotencyConflictError("only pending candidates can be rejected")
            connection.execute(
                text(
                    """
                    UPDATE candidate_rules SET status = 'REJECTED',
                        reviewer_id = :reviewer, reviewed_at = now()
                    WHERE candidate_id = :id
                    """
                ),
                {"id": candidate_id, "reviewer": reviewer_id},
            )
            self._insert_event(
                connection,
                request_id=candidate["source_request_id"],
                event_type="RULE_CANDIDATE_REJECTED",
                actor=reviewer_id,
                payload={"candidate_id": candidate_id},
                dedupe_key=f"candidate-rejected:{candidate_id}",
            )

    def publish_rule(self, candidate_id: int, *, reviewer_id: str) -> str:
        with self.engine.begin() as connection:
            candidate = connection.execute(
                text("SELECT * FROM candidate_rules WHERE candidate_id = :id FOR UPDATE"),
                {"id": candidate_id},
            ).mappings().first()
            if candidate is None:
                raise LookupError(f"unknown candidate: {candidate_id}")
            if candidate["status"] != "APPROVED" or candidate["published_at"]:
                raise IdempotencyConflictError("candidate is not approved for publication")
            base = connection.execute(
                text(
                    """
                    SELECT * FROM policy_rules
                    WHERE rule_id = :rule_id AND source_document_id = :source
                      AND enabled = true AND effective_to IS NULL
                      AND effective_from < :effective_from
                    ORDER BY effective_from DESC LIMIT 1 FOR UPDATE
                    """
                ),
                {
                    "rule_id": candidate["target_rule_id"],
                    "source": candidate["source_document_id"],
                    "effective_from": candidate["effective_from"],
                },
            ).mappings().first()
            if base is None:
                raise IdempotencyConflictError("published rule changed since candidate review")
            replay_rule(
                base["rule_type"],
                base["parameters"],
                candidate["parameters"],
                candidate["replay_cases"],
            )
            row_id = f"candidate-{candidate_id}"
            connection.execute(
                text("UPDATE policy_rules SET effective_to = :end WHERE id = :id"),
                {
                    "id": base["id"],
                    "end": candidate["effective_from"] - timedelta(days=1),
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO policy_rules (
                        id, rule_id, rule_version, expense_type, rule_type,
                        parameters, source_document_id, effective_from, enabled
                    ) VALUES (
                        :id, :rule_id, :version, :expense_type, :rule_type,
                        CAST(:parameters AS jsonb), :source, :effective_from, true
                    )
                    """
                ),
                {
                    "id": row_id,
                    "rule_id": base["rule_id"],
                    "version": f"P12-{candidate_id}",
                    "expense_type": base["expense_type"],
                    "rule_type": base["rule_type"],
                    "parameters": _json(candidate["parameters"]),
                    "source": base["source_document_id"],
                    "effective_from": candidate["effective_from"],
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE candidate_rules SET published_rule_row_id = :new_id,
                        prior_rule_row_id = :prior_id,
                        prior_effective_to = :prior_end, published_at = now()
                    WHERE candidate_id = :id
                    """
                ),
                {
                    "id": candidate_id,
                    "new_id": row_id,
                    "prior_id": base["id"],
                    "prior_end": base["effective_to"],
                },
            )
            self._insert_event(
                connection,
                request_id=candidate["source_request_id"],
                event_type="RULE_PUBLISHED",
                actor=reviewer_id,
                payload={"candidate_id": candidate_id, "rule_id": base["rule_id"]},
                dedupe_key=f"rule-published:{candidate_id}",
            )
            return base["rule_id"]

    def rollback_rule(self, candidate_id: int, *, reviewer_id: str) -> None:
        with self.engine.begin() as connection:
            candidate = connection.execute(
                text("SELECT * FROM candidate_rules WHERE candidate_id = :id FOR UPDATE"),
                {"id": candidate_id},
            ).mappings().first()
            if candidate is None:
                raise LookupError(f"unknown candidate: {candidate_id}")
            if candidate["status"] != "APPROVED" or not candidate["published_at"]:
                raise IdempotencyConflictError("only a current published candidate can roll back")
            disabled = connection.execute(
                text(
                    """
                    UPDATE policy_rules SET enabled = false
                    WHERE id = :id AND enabled = true AND effective_to IS NULL
                    """
                ),
                {"id": candidate["published_rule_row_id"]},
            )
            if disabled.rowcount != 1:
                raise IdempotencyConflictError("a newer revision exists; rollback it first")
            connection.execute(
                text("UPDATE policy_rules SET effective_to = :end WHERE id = :id"),
                {
                    "id": candidate["prior_rule_row_id"],
                    "end": candidate["prior_effective_to"],
                },
            )
            connection.execute(
                text("UPDATE candidate_rules SET status = 'SUPERSEDED' WHERE candidate_id = :id"),
                {"id": candidate_id},
            )
            self._insert_event(
                connection,
                request_id=candidate["source_request_id"],
                event_type="RULE_ROLLED_BACK",
                actor=reviewer_id,
                payload={"candidate_id": candidate_id, "rule_id": candidate["target_rule_id"]},
                dedupe_key=f"rule-rolled-back:{candidate_id}",
            )
