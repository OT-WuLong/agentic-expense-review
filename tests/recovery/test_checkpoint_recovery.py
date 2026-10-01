import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app import approval_service
from app.database import (
    DEFAULT_DATABASE_URL,
    IdempotencyConflictError,
    PostgresStore,
    finalization_event_types,
)
from app.graph.workflow import (
    _CHECKPOINT_ALLOWED_TYPES,
    _checkpoint_url,
    _setup_checkpointer_once,
)
from app.models import Applicant, Application, ApprovalStatus, HumanReviewSubmission
from scripts.evaluate_recovery import count_duplicate_side_effects, run_recovery_smoke


def test_review_lock_contention_does_not_record_a_false_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    @contextmanager
    def busy_slot(_: str):
        yield False

    class EventStore:
        def record_event(self, **values: object) -> None:
            events.append(str(values["event_type"]))

        def close(self) -> None:
            pass

    monkeypatch.setattr(approval_service, "_request_slot", busy_slot)
    monkeypatch.setattr(approval_service, "PostgresStore", EventStore)
    submission = HumanReviewSubmission(
        action="REJECT",
        operator_id="FIN-001",
        reviewer_role="FINANCE_REVIEWER",
        reason="审慎处理",
        idempotency_key="review-contention",
    )

    approval_service.resume_review_background("REQ-REVIEW-CONTENTION", None, submission)  # type: ignore[arg-type]

    assert events == []


@pytest.mark.integration
def test_postgres_checkpoint_recovery_is_idempotent() -> None:
    url = make_url(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)).update_query_dict(
        {"connect_timeout": "1"}
    )
    store = PostgresStore(url.render_as_string(hide_password=False))
    try:
        store.ping()
    except SQLAlchemyError:
        pytest.skip("requires a live PostgreSQL integration database")
    finally:
        store.close()
    assert run_recovery_smoke()["passed"] is True


@pytest.mark.integration
def test_request_lock_excludes_other_database_sessions() -> None:
    first = PostgresStore()
    second = PostgresStore()
    try:
        try:
            first.ping()
        except SQLAlchemyError:
            pytest.skip("requires a live PostgreSQL integration database")
        with (
            first.request_lock("REQ-LOCK-TEST") as first_acquired,
            second.request_lock("REQ-LOCK-TEST") as second_acquired,
        ):
            assert first_acquired is True
            assert second_acquired is False
        with second.request_lock("REQ-LOCK-TEST") as acquired_after_release:
            assert acquired_after_release is True
    finally:
        first.close()
        second.close()


@pytest.mark.integration
def test_two_reviewers_cannot_claim_the_same_approval() -> None:
    store = PostgresStore()
    request_id = f"REQ-REVIEW-CLAIM-{uuid4().hex}"
    available = False
    try:
        try:
            store.ping()
        except SQLAlchemyError:
            pytest.skip("requires a live PostgreSQL integration database")
        available = True
        store.create_approval(
            request_id=request_id,
            thread_id=f"approval:{request_id}",
            idempotency_key=f"create:{request_id}",
            applicant=Applicant(
                employee_id="EMP-REVIEW-CLAIM",
                department_id="DEPT-SYN-RD",
                display_name="复核并发测试",
            ),
            application=Application(
                expense_type="交通",
                currency="CNY",
                amount="10.00",
                occurred_on="2026-09-01",
                submitted_on="2026-09-02",
                description="复核并发测试",
            ),
            documents=[],
        )
        with store.engine.begin() as connection:
            connection.execute(
                text("UPDATE approval_requests SET status = 'HUMAN_PENDING' WHERE request_id = :id"),
                {"id": request_id},
            )

        def attempt(index: int) -> tuple[bool, str] | str:
            submission = HumanReviewSubmission(
                action="APPROVE" if index == 0 else "REJECT",
                operator_id=f"FIN-{index}",
                reviewer_role="FINANCE_REVIEWER",
                reason=f"第 {index} 份意见",
                idempotency_key=f"review-{index}",
            )
            try:
                return store.claim_human_review(
                    request_id, submission, expected_status="HUMAN_PENDING"
                )
            except IdempotencyConflictError:
                return "CONFLICT"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, range(2)))
        assert outcomes.count((True, "HUMAN_PENDING")) == 1
        assert outcomes.count("CONFLICT") == 1
        accepted = store.accepted_human_review(request_id)
        assert accepted is not None
        assert accepted.idempotency_key in {"review-0", "review-1"}
    finally:
        if available:
            with store.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM audit_events WHERE request_id = :id"), {"id": request_id}
                )
                connection.execute(
                    text("DELETE FROM approval_requests WHERE request_id = :id"),
                    {"id": request_id},
                )
        store.close()


@pytest.mark.integration
def test_workflow_failure_is_visible_and_retryable() -> None:
    store = PostgresStore()
    request_id = f"REQ-FAILURE-{uuid4().hex}"
    available = False
    try:
        try:
            store.ping()
        except SQLAlchemyError:
            pytest.skip("requires a live PostgreSQL integration database")
        available = True
        store.create_approval(
            request_id=request_id,
            thread_id=f"approval:{request_id}",
            idempotency_key=f"create:{request_id}",
            applicant=Applicant(
                employee_id="EMP-FAILURE-TEST",
                department_id="DEPT-SYN-RD",
                display_name="故障恢复测试",
            ),
            application=Application(
                expense_type="交通",
                currency="CNY",
                amount="10.00",
                occurred_on="2026-09-01",
                submitted_on="2026-09-02",
                description="故障恢复测试",
            ),
            documents=[],
        )

        assert store.mark_workflow_failed(
            request_id,
            error="RuntimeError",
            message="dependency unavailable",
            failure_key="failure-1",
            trace_id="TRACE-FAILURE-1",
        )
        assert store.approval_record(request_id)["status"] == ApprovalStatus.SYSTEM_ERROR.value  # type: ignore[index]
        assert store.audit_event_counts(request_id)["WORKFLOW_FAILED"] == 1

        assert store.mark_workflow_retrying(
            request_id,
            actor="FIN-FAILURE-TEST",
            idempotency_key="retry-1",
        )
        assert store.approval_record(request_id)["status"] == ApprovalStatus.RUNNING.value  # type: ignore[index]
        assert store.audit_event_counts(request_id)["WORKFLOW_RETRY_REQUESTED"] == 1
    finally:
        if available:
            with store.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM audit_events WHERE request_id = :request_id"),
                    {"request_id": request_id},
                )
                connection.execute(
                    text("DELETE FROM approval_requests WHERE request_id = :request_id"),
                    {"request_id": request_id},
                )
        store.close()


def test_checkpoint_contract_is_derived_and_driver_independent() -> None:
    allowed = set(_CHECKPOINT_ALLOWED_TYPES)
    assert ("app.models", "Application") in allowed
    assert ("app.agents.contracts", "AgentStep") in allowed
    assert ("app.models", "HumanReviewPayload") not in allowed
    assert ("app.models", "HumanReviewSubmission") not in allowed
    assert _checkpoint_url(
        "postgresql+psycopg2://user:password@localhost/database"
    ).startswith("postgresql://")


def test_checkpoint_setup_runs_once_per_database_url() -> None:
    class FakeCheckpointer:
        def __init__(self) -> None:
            self.calls = 0

        def setup(self) -> None:
            self.calls += 1

    first = FakeCheckpointer()
    second = FakeCheckpointer()
    url = "postgresql://checkpoint-once.invalid/database"

    _setup_checkpointer_once(url, first)  # type: ignore[arg-type]
    _setup_checkpointer_once(url, second)  # type: ignore[arg-type]

    assert first.calls == 1
    assert second.calls == 0


def test_system_error_only_requests_an_operations_alert() -> None:
    assert finalization_event_types(ApprovalStatus.SYSTEM_ERROR, None) == (
        "APPROVAL_FINALIZED",
        "OPERATIONS_ALERT_REQUESTED",
    )


def test_duplicate_side_effects_are_measured_from_event_counts() -> None:
    assert count_duplicate_side_effects(
        {
            "one": {"passed": True, "event_counts": {"EVENT": 2}},
            "two": {"passed": True, "event_counts": {"EVENT": 3}},
        }
    ) == 3
