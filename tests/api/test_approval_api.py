from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main
from app.api_models import DemoIdentity
from app.approval_service import ApprovalService
from app.database import IdempotencyConflictError
from app.graph.state import validate_state
from app.main import app, get_store
from app.models import (
    ApprovalStatus,
    DecisionReason,
    HumanAction,
    HumanActionType,
    HumanReviewSubmission,
    Recommendation,
    RiskLevel,
)


class FakeStore:
    def __init__(self, *, connect_timeout: int | None = None) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.review_claims: dict[str, dict[str, object]] = {}
        self.events = [
            {
                "event_id": 1,
                "request_id": "REQ-API-001",
                "event_type": "APPROVAL_CREATED",
                "actor": "SYSTEM",
                "payload": {},
                "created_at": datetime(2026, 9, 22, tzinfo=UTC),
            },
            {
                "event_id": 2,
                "request_id": "REQ-API-001",
                "event_type": "APPROVAL_FINALIZED",
                "actor": "SYSTEM",
                "payload": {"status": "COMPLETED"},
                "created_at": datetime(2026, 9, 22, 0, 1, tzinfo=UTC),
            },
        ]

    def close(self) -> None:
        pass

    @contextmanager
    def request_lock(self, _: str):
        yield True

    def extracted_fields(self, _: str) -> list[object]:
        return []

    def create_approval(self, **values: Any) -> bool:
        request_id = values["request_id"]
        if request_id in self.records:
            return False
        application = values["application"]
        applicant = values["applicant"]
        self.records[request_id] = {
            "request_id": request_id,
            "thread_id": values["thread_id"],
            "status": "RUNNING",
            "recommendation": None,
            "risk_level": None,
            "final_decision": None,
            "employee_id": applicant.employee_id,
            "department_id": applicant.department_id,
            "expense_type": application.expense_type.value,
            "currency": application.currency,
            "amount": application.amount,
            "occurred_on": application.occurred_on,
            "submitted_on": application.submitted_on,
            "application": application.model_dump(mode="json"),
            "documents": [],
            "created_at": datetime(2026, 9, 22, tzinfo=UTC),
            "updated_at": datetime(2026, 9, 22, tzinfo=UTC),
        }
        return True

    def upsert_extracted_fields(self, *_: object) -> None:
        pass

    def approval_record(
        self, request_id: str, *, department_ids: list[str] | None = None,
        employee_id: str | None = None,
    ) -> dict[str, object] | None:
        record = self.records.get(request_id)
        if record is None:
            return None
        if department_ids is not None and record.get("department_id") not in department_ids:
            return None
        if employee_id is not None and record.get("employee_id") != employee_id:
            return None
        return record

    def approval_detail(self, request_id: str) -> dict[str, object] | None:
        return self.records.get(request_id)

    def claim_human_review(
        self, request_id: str, submission: HumanReviewSubmission, *, expected_status: str | None
    ) -> tuple[bool, str]:
        status = str(self.records[request_id]["status"])
        payload = submission.model_dump(mode="json", exclude_none=True)
        existing = self.review_claims.get(request_id)
        if existing is not None:
            if existing != payload:
                raise IdempotencyConflictError("另一份人工复核已被受理")
            return False, status
        if expected_status is None or status != expected_status:
            raise ValueError("当前审批不接受新的人工复核")
        self.review_claims[request_id] = payload
        return True, status

    def accepted_human_review(self, request_id: str) -> HumanReviewSubmission | None:
        payload = self.review_claims.get(request_id)
        return HumanReviewSubmission.model_validate(payload) if payload else None

    def record_event(
        self, *, request_id: str | None, event_type: str, actor: str,
        payload: dict[str, object], dedupe_key: str,
    ) -> None:
        self.events.append({
            "event_id": len(self.events) + 1,
            "request_id": request_id,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
            "created_at": datetime(2026, 9, 22, tzinfo=UTC),
        })

    def list_approvals(self, **_: object) -> tuple[list[dict[str, object]], int]:
        items = list(self.records.values())
        return items, len(items)

    def audit_events_after(
        self, request_id: str, *, after_event_id: int = 0, limit: int = 200
    ) -> list[dict[str, object]]:
        return [
            item
            for item in self.events
            if item["request_id"] == request_id and item["event_id"] > after_event_id
        ][:limit]

    def policy_upload_events(self) -> list[dict[str, object]]:
        return []

    def policy_rules(self, **_: object) -> list[object]:
        return []

    def mark_workflow_retrying(
        self, request_id: str, *, actor: str, idempotency_key: str,
        expected_status: str = ApprovalStatus.SYSTEM_ERROR.value,
        stale_before: datetime | None = None,
    ) -> bool:
        record = self.records.get(request_id)
        if record is None or record["status"] != expected_status:
            return False
        record["status"] = ApprovalStatus.RUNNING.value
        return True


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeStore]:
    monkeypatch.setenv("APP_SESSION_SECRET", "p13-test-session-secret-12345678901234567890")
    monkeypatch.setenv(
        "APP_AUTH_USERS_JSON",
        json.dumps(
            [
                {
                    "token": "p13-reviewer-token-123456789012345",
                    "actor_id": "FIN-001", "display_name": "复核员",
                    "role": "FINANCE_REVIEWER",
                    "department_ids": ["DEPT-001", "DEPT-SYN-RD", "DEPT-SYN-EAST-SALES", "DEPT-SYN-MARKETING"],
                },
                {
                    "token": "p13-applicant-token-12345678901234",
                    "actor_id": "EMP-001", "display_name": "申请人",
                    "role": "APPLICANT", "department_ids": ["DEPT-001"],
                },
            ]
        ),
    )
    store = FakeStore()
    store.records["REQ-API-001"] = {
        "request_id": "REQ-API-001",
        "thread_id": "approval:REQ-API-001",
        "status": "COMPLETED",
        "recommendation": "PASS_RECOMMENDED",
        "risk_level": "LOW",
        "final_decision": None,
        "employee_id": "EMP-001",
        "department_id": "DEPT-001",
        "expense_type": "交通",
        "currency": "CNY",
        "amount": "20.00",
        "occurred_on": "2026-09-01",
        "submitted_on": "2026-09-02",
        "application": {"description": "客户拜访"},
        "documents": [],
        "created_at": datetime(2026, 9, 22, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 22, tzinfo=UTC),
    }
    app.dependency_overrides[get_store] = lambda: store
    monkeypatch.setattr(main, "PostgresStore", FakeStore)
    monkeypatch.setattr(main, "run_approval_background", lambda *_: None)
    monkeypatch.setattr(main, "checkpoint_state", lambda _: None)

    async def no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    try:
        yield TestClient(app), store
    finally:
        app.dependency_overrides.clear()


def _headers(role: str = "reviewer") -> dict[str, str]:
    token = (
        "p13-reviewer-token-123456789012345"
        if role == "reviewer"
        else "p13-applicant-token-12345678901234"
    )
    return {"Authorization": f"Bearer {token}"}


def test_denial_response_does_not_wait_on_offline_audit_database(
    api: tuple[TestClient, FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    timeouts: list[int | None] = []

    class OfflineStore:
        def __init__(self, *, connect_timeout: int | None = None) -> None:
            timeouts.append(connect_timeout)

        def record_event(self, **_: object) -> None:
            if timeouts[-1] is None:
                time.sleep(0.9)
            raise ConnectionError("database offline")

        def close(self) -> None:
            pass

    monkeypatch.setattr(main, "PostgresStore", OfflineStore)
    started = time.perf_counter()
    response = client.get("/api/v1/no-such-route")
    elapsed = time.perf_counter() - started

    assert response.status_code == 404
    assert timeouts == [1]
    assert elapsed < 0.75


def test_openapi_exposes_the_complete_p10_surface() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/api/v1/approvals",
        "/api/v1/approvals/{request_id}",
        "/api/v1/approvals/{request_id}/documents",
        "/api/v1/approvals/{request_id}/evidence",
        "/api/v1/approvals/{request_id}/events",
        "/api/v1/approvals/{request_id}/review",
        "/api/v1/approvals/{request_id}/retry",
        "/api/v1/approvals/{request_id}/audit",
        "/api/v1/policies",
        "/api/v1/rules/{rule_id}/publish",
    }
    assert expected <= set(paths)


def test_auth_errors_are_consistent_and_echo_request_id(api: tuple[TestClient, FakeStore]) -> None:
    client, _ = api
    response = client.get("/api/v1/approvals", headers={"X-Request-ID": "HTTP-TEST-001"})

    assert response.status_code == 401
    assert response.headers["X-Request-ID"] == "HTTP-TEST-001"
    assert response.json()["error"] == {
        "code": "HTTP_ERROR",
        "message": "请先登录",
        "request_id": "HTTP-TEST-001",
        "details": None,
    }


def test_create_fixture_returns_before_background_work(
    api: tuple[TestClient, FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store = api
    calls: list[str] = []
    monkeypatch.setattr(
        main,
        "run_approval_background",
        lambda state, _context: calls.append(state["request_id"]),
    )
    response = client.post(
        "/api/v1/approvals",
        headers={**_headers(), "Idempotency-Key": "create-api-test"},
        json={
            "fixture_case_id": "GC-C-DINING-VERSION-CONFLICT-HUMAN",
            "request_id": "REQ-API-CREATE-001",
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "RUNNING"
    assert store.records["REQ-API-CREATE-001"]["status"] == "RUNNING"
    assert calls == ["REQ-API-CREATE-001"]


def test_create_uses_http_request_id_as_workflow_trace(
    api: tuple[TestClient, FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    traces: list[str] = []
    monkeypatch.setattr(
        main,
        "run_approval_background",
        lambda state, _context: traces.append(state["trace_id"]),
    )

    response = client.post(
        "/api/v1/approvals",
        headers={
            **_headers(),
            "Idempotency-Key": "create-trace-test",
            "X-Request-ID": "HTTP-CREATE-TRACE-002",
        },
        json={
            "fixture_case_id": "GC-A-TRANSPORT-COMMUTE-REJECT",
            "request_id": "REQ-API-TRACE-001",
        },
    )

    assert response.status_code == 202
    assert traces == ["HTTP-CREATE-TRACE-002"]


def test_manual_creation_cannot_claim_fixture_attachments(
    api: tuple[TestClient, FakeStore],
) -> None:
    client, _ = api
    response = client.post(
        "/api/v1/approvals",
        headers={**_headers(), "Idempotency-Key": "manual-document-claim"},
        json={
            "request": {
                "request_id": "REQ-API-MANUAL-DOC-001",
                "applicant": {
                    "employee_id": "EMP-001",
                    "department_id": "DEPT-001",
                    "display_name": "测试员工",
                },
                "application": {
                    "expense_type": "餐饮",
                    "currency": "CNY",
                    "amount": "100.00",
                    "occurred_on": "2026-06-15",
                    "submitted_on": "2026-06-16",
                    "description": "客户餐饮",
                    "attendee_count": 1,
                },
                "documents": [
                    {
                        "document_id": "DOC-GC-C-MEAL-001",
                        "document_type": "MEAL_INVOICE",
                        "media_type": "application/pdf",
                        "synthetic": False,
                    }
                ],
            }
        },
    )

    assert response.status_code == 422


def test_list_is_paginated(api: tuple[TestClient, FakeStore]) -> None:
    client, _ = api
    response = client.get("/api/v1/approvals?page=1&page_size=20", headers=_headers())

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["request_id"] == "REQ-API-001"


def test_approval_services_share_validated_static_fixtures(
    api: tuple[TestClient, FakeStore],
) -> None:
    _, store = api
    first = ApprovalService(store)  # type: ignore[arg-type]
    second = ApprovalService(store)  # type: ignore[arg-type]

    assert first.chunks is second.chunks
    assert first.cases is second.cases
    assert first.manifest is second.manifest


def test_detail_exposes_checkpoint_projection_consistency(
    api: tuple[TestClient, FakeStore],
) -> None:
    client, _ = api
    response = client.get("/api/v1/approvals/REQ-API-001", headers=_headers())

    assert response.status_code == 200
    assert response.json()["consistency"] == {
        "status": "MISSING_CHECKPOINT",
        "mismatched_fields": [],
    }


def test_sse_resumes_strictly_after_last_event_id(api: tuple[TestClient, FakeStore]) -> None:
    client, _ = api
    with client.stream(
        "GET",
        "/api/v1/approvals/REQ-API-001/events",
        headers={**_headers(), "Last-Event-ID": "1"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "id: 1\n" not in body
    assert "id: 2\n" in body
    assert "event: APPROVAL_FINALIZED" in body
    assert '"request_id":"REQ-API-001"' in body


def test_review_requires_server_side_reviewer_role(api: tuple[TestClient, FakeStore]) -> None:
    client, _ = api
    response = client.post(
        "/api/v1/approvals/REQ-API-001/review",
        headers=_headers("applicant"),
        json={
            "action": "REJECT",
            "reason": "人工核实后驳回",
            "idempotency_key": "review-api-test",
        },
    )

    assert response.status_code == 403


def test_review_rejects_a_second_decision_before_background_runs(
    api: tuple[TestClient, FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store = api
    request_id = "REQ-API-REVIEW-CLAIM"
    _, base, _, _ = ApprovalService(store).prepare(
        None, "GC-C-DINING-VERSION-CONFLICT-HUMAN", request_id
    )
    state = validate_state(
        {
            **base,
            "status": ApprovalStatus.HUMAN_PENDING,
            "recommendation": Recommendation.HUMAN_REVIEW,
            "risk_level": RiskLevel.MEDIUM,
            "decision_reasons": [
                DecisionReason(code="POLICY_CONFLICT", message="制度版本冲突")
            ],
            "pending_human_action": HumanAction(
                action=HumanActionType.REVIEW, reason="制度版本冲突"
            ),
        }
    )
    store.records[request_id] = {
        "request_id": request_id,
        "status": ApprovalStatus.HUMAN_PENDING.value,
        "employee_id": "EMP-SYN-003",
        "department_id": "DEPT-SYN-MARKETING",
    }
    monkeypatch.setattr(
        main, "checkpoint_state",
        lambda _: {"state": state, "next_nodes": ["human_review"], "interrupted": True},
    )
    scheduled: list[str] = []
    monkeypatch.setattr(
        main,
        "resume_review_background",
        lambda *args: scheduled.append(args[2].idempotency_key),
    )

    first_payload = {"action": "APPROVE", "reason": "确认通过", "idempotency_key": "review-first"}
    first = client.post(
        f"/api/v1/approvals/{request_id}/review",
        headers=_headers(),
        json=first_payload,
    )
    replay = client.post(
        f"/api/v1/approvals/{request_id}/review", headers=_headers(), json=first_payload
    )
    competing = client.post(
        f"/api/v1/approvals/{request_id}/review",
        headers=_headers(),
        json={"action": "REJECT", "reason": "决定驳回", "idempotency_key": "review-second"},
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert competing.status_code == 409
    assert scheduled == ["review-first", "review-first"]

    retry = client.post(
        f"/api/v1/approvals/{request_id}/retry",
        headers={**_headers(), "Idempotency-Key": "retry-accepted-review"},
    )
    assert retry.status_code == 202
    assert retry.json()["status"] == "RESUMING"
    assert scheduled == ["review-first", "review-first", "review-first"]

    store.records[request_id]["status"] = ApprovalStatus.COMPLETED.value
    monkeypatch.setattr(
        main, "checkpoint_state",
        lambda _: {"state": {}, "next_nodes": [], "interrupted": False},
    )
    finished_replay = client.post(
        f"/api/v1/approvals/{request_id}/review", headers=_headers(), json=first_payload
    )
    assert finished_replay.status_code == 200
    assert finished_replay.json()["status"] == ApprovalStatus.COMPLETED.value


def test_review_rejects_new_submission_after_approval_is_final(
    api: tuple[TestClient, FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.setattr(
        main, "checkpoint_state",
        lambda _: {"state": {}, "next_nodes": [], "interrupted": False},
    )

    response = client.post(
        "/api/v1/approvals/REQ-API-001/review",
        headers=_headers(),
        json={"action": "REJECT", "reason": "新的复核意见", "idempotency_key": "late-review"},
    )

    assert response.status_code == 409


def test_request_documents_rejects_approve_before_background_resume(
    api: tuple[TestClient, FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store = api
    request_id = "REQ-API-REQUEST-DOCUMENTS"
    service = ApprovalService(store)  # type: ignore[arg-type]
    _, base, _, _ = service.prepare(
        None,
        "GC-C-DINING-VERSION-CONFLICT-HUMAN",
        request_id,
    )
    state = validate_state(
        {
            **base,
            "status": ApprovalStatus.INSUFFICIENT_EVIDENCE,
            "recommendation": Recommendation.HUMAN_REVIEW,
            "risk_level": RiskLevel.MEDIUM,
            "decision_reasons": [DecisionReason(code="MISSING_DOCUMENTS", message="缺少发票")],
            "pending_human_action": HumanAction(
                action=HumanActionType.REQUEST_DOCUMENTS,
                reason="缺少发票",
                missing_items=["发票"],
            ),
        }
    )
    store.records[request_id] = {
        "request_id": request_id,
        "status": ApprovalStatus.INSUFFICIENT_EVIDENCE.value,
        "employee_id": "EMP-SYN-003",
        "department_id": "DEPT-SYN-MARKETING",
    }
    monkeypatch.setattr(
        main,
        "checkpoint_state",
        lambda _: {
            "state": state,
            "next_nodes": ["human_review"],
            "interrupted": True,
        },
    )

    response = client.post(
        f"/api/v1/approvals/{request_id}/review",
        headers=_headers(),
        json={
            "action": "APPROVE",
            "reason": "误点确认",
            "idempotency_key": "invalid-request-documents-approve",
        },
    )

    assert response.status_code == 409
    edit_to_pass = client.post(
        f"/api/v1/approvals/{request_id}/review",
        headers=_headers(),
        json={
            "action": "EDIT",
            "recommendation": "PASS_RECOMMENDED",
            "reason": "仍然尝试通过",
            "idempotency_key": "invalid-request-documents-edit-pass",
        },
    )
    assert edit_to_pass.status_code == 409


def test_failed_workflow_can_resume_from_checkpoint(
    api: tuple[TestClient, FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store = api
    request_id = "REQ-API-RETRY"
    service = ApprovalService(store)  # type: ignore[arg-type]
    _, state, _, _ = service.prepare(
        None,
        "GC-A-TRANSPORT-COMMUTE-REJECT",
        request_id,
    )
    store.records[request_id] = {
        "request_id": request_id,
        "status": ApprovalStatus.SYSTEM_ERROR.value,
        "employee_id": "EMP-SYN-001",
        "department_id": "DEPT-SYN-RD",
    }
    monkeypatch.setattr(
        main,
        "checkpoint_state",
        lambda _: {
            "state": state,
            "next_nodes": ["supervisor"],
            "interrupted": False,
            "allowed_actions": [],
        },
    )
    resumed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        main,
        "run_approval_background",
        lambda current, _context, *, renew_deadline=False: resumed.append(
            (current["request_id"], renew_deadline)
        ),
    )

    response = client.post(
        f"/api/v1/approvals/{request_id}/retry",
        headers={**_headers(), "Idempotency-Key": "retry-api-test"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == ApprovalStatus.RUNNING.value
    assert resumed == [(request_id, True)]


@pytest.mark.parametrize(
    ("field", "invalid"),
    [("expense_type", "其他"), ("effective_from", "2026-99-99")],
)
def test_policy_upload_rejects_invalid_metadata_without_reserving_id(
    api: tuple[TestClient, FakeStore], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any, field: str, invalid: str,
) -> None:
    client, store = api
    app.dependency_overrides[main.rule_admin] = lambda: DemoIdentity(
        actor_id="ADMIN-001", display_name="规则管理员", role="RULE_ADMIN"
    )
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(store, "record_event", lambda **_: None, raising=False)
    document_id = f"POL-INVALID-{field}"
    metadata = {
        "document_id": document_id,
        "title": "测试制度",
        "expense_type": "交通",
        "version": "1.0",
        "effective_from": "2026-09-28",
    }
    pdf = b"%PDF-1.4\n%%EOF"

    rejected = client.post(
        "/api/v1/policies",
        data={**metadata, field: invalid},
        files={"file": ("policy.pdf", pdf, "application/pdf")},
    )
    accepted = client.post(
        "/api/v1/policies",
        data=metadata,
        files={"file": ("policy.pdf", pdf, "application/pdf")},
    )

    assert rejected.status_code == 422
    assert accepted.status_code == 201
