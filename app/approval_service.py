"""Application service joining the P10 HTTP API to the P09 durable graph."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from functools import cache
from pathlib import Path
from threading import Lock
from typing import Any

from langgraph.types import Command
from pydantic import BaseModel

from app.agents.contracts import ToolName
from app.agents.model import StructuredChatClient
from app.database import PostgresStore
from app.graph.approval import ApprovalRuntime, allowed_human_actions
from app.graph.state import ApprovalState, validate_state
from app.graph.workflow import approval_config, open_persistent_approval_graph
from app.ingestion.models import DocumentChunk
from app.invoice_ingestion import parse_uploaded_invoice
from app.memory import write_memory
from app.models import (
    Applicant,
    Application,
    ApprovalInput,
    ApprovalStatus,
    Document,
    EvidenceItem,
    EvidenceMetrics,
    EvidenceSource,
    ExpenseType,
    ExtractedField,
    ExtractionStatus,
    HumanReviewSubmission,
)
from app.retrieval import HybridPolicyIndex, QwenEmbeddingClient, QwenRerankerClient
from app.rules import required_rule_ids
from app.structured import DEMO_STRUCTURED_SNAPSHOT_ID
from app.tools import ToolExecutionContext, build_tool_registry

ROOT = Path(__file__).resolve().parents[1]
INVOICE_DOCUMENT_TYPES = {
    ExpenseType.TRANSPORT: "TAXI_INVOICE",
    ExpenseType.LODGING: "HOTEL_INVOICE",
    ExpenseType.DINING: "MEAL_INVOICE",
}
CHUNKS_PATH = ROOT / "data/fixtures/p04_chunks.jsonl"
CASES_PATH = ROOT / "data/fixtures/golden_cases.json"
MANIFEST_PATH = ROOT / "data/fixtures/source_manifest.json"
ACTIVE_CATALOG_PATH = ROOT / "data/fixtures/active_catalog.json"
MILVUS_URI = os.getenv("MILVUS_URI", "http://localhost:19530")
POLICY_RETRIEVAL_MODE = os.getenv("POLICY_RETRIEVAL_MODE", "dense")
DEFAULT_LIMITS = {
    "max_agent_steps": 8,
    "max_retrieval_rounds": 3,
    "max_query_rewrites": 2,
}
WORKFLOW_RUN_WINDOW = timedelta(minutes=7)


def saved_invoice_for_retry(record: Mapping[str, object]) -> tuple[ApprovalInput, Path] | None:
    """Rebuild a pre-graph request from its server-owned saved invoice."""

    application = Application.model_validate(record["application"])
    document_type = INVOICE_DOCUMENT_TYPES[application.expense_type]
    upload_root = (ROOT / "tmp/uploads/documents").resolve()
    for item in record.get("documents", []):
        if not isinstance(item, Mapping) or item.get("document_type") != document_type:
            continue
        uri = item.get("storage_uri")
        if item.get("synthetic") or not isinstance(uri, str):
            continue
        path = (ROOT / uri).resolve()
        if not path.is_relative_to(upload_root) or not path.is_file():
            continue
        document = Document.model_validate(
            {key: item[key] for key in ("document_id", "document_type", "media_type")}
        )
        # ponytail: approval_requests stores IDs but not the display name; rules use the IDs.
        applicant = Applicant(
            employee_id=str(record["employee_id"]),
            department_id=str(record["department_id"]),
            display_name=str(record["employee_id"]),
        )
        return (
            ApprovalInput(
                request_id=str(record["request_id"]),
                applicant=applicant,
                application=application,
                documents=[document],
            ),
            path,
        )
    return None
_RESOURCE_LOCK = Lock()
_ACTIVE_REQUESTS: set[str] = set()
_SHARED_INDEX: HybridPolicyIndex | None = None


def jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@cache
def _static_fixtures() -> tuple[
    tuple[DocumentChunk, ...], dict[str, dict[str, Any]], dict[str, Any]
]:
    chunks = tuple(
        DocumentChunk.model_validate_json(line)
        for line in CHUNKS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    dataset = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = {case["case_id"]: case for case in dataset["cases"]}
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return chunks, cases, manifest


@cache
def active_catalog() -> dict[str, Any]:
    return json.loads(ACTIVE_CATALOG_PATH.read_text(encoding="utf-8"))


class ApprovalService:
    def __init__(self, store: PostgresStore) -> None:
        self.store = store
        self.chunks, self.cases, self.manifest = _static_fixtures()

    def prepare(
        self,
        payload: ApprovalInput | None,
        fixture_case_id: str | None,
        request_id: str | None,
        *,
        trace_id: str | None = None,
        parsed_fields: list[ExtractedField] | None = None,
        parsed_evidence: list[EvidenceItem] | None = None,
    ) -> tuple[ApprovalInput, ApprovalState, ToolExecutionContext, list[ExtractedField]]:
        case: dict[str, Any] | None = None
        source_document_ids: dict[str, str] = {}
        if fixture_case_id:
            try:
                case = self.cases[fixture_case_id]
            except KeyError as exc:
                raise LookupError(f"unknown fixture case: {fixture_case_id}") from exc
            source = ApprovalInput.model_validate(case["input"])
            assert request_id is not None
            documents = payload.documents if payload is not None else [
                document.model_copy(update={"document_id": f"{document.document_id}-{request_id}"})
                for document in source.documents
            ]
            source_document_ids = {
                original.document_id: cloned.document_id
                for original, cloned in zip(source.documents, documents, strict=True)
            }
            approval_input = source.model_copy(
                update={"request_id": request_id, "documents": documents}
            )
            extracted_fields = [
                field.model_copy(
                    update={
                        "document_id": source_document_ids.get(
                            field.document_id or "", field.document_id
                        )
                    }
                )
                for field in self.store.extracted_fields(source.request_id)
            ]
            context_data = case["fixture_context"]
            limits = case["hard_limits"]
            allowed_tools = [ToolName(item) for item in case["allowed_tools"]]
        else:
            if payload is None:
                raise ValueError("approval input is required")
            approval_input = payload
            extracted_fields = parsed_fields or []
            limits = DEFAULT_LIMITS
            context_data = {
                "policy_catalog_snapshot_id": active_catalog()["policy_catalog_snapshot_id"],
                "structured_data_snapshot_id": active_catalog()["structured_data_snapshot_id"],
            }
            allowed_tools = [
                ToolName.POLICY_SEARCH,
                ToolName.CASE_SEARCH,
                ToolName.RULE_SEARCH,
            ]
            if approval_input.application.expense_type == ExpenseType.LODGING:
                allowed_tools.append(ToolName.STRUCTURED_LOOKUP)

        catalog_snapshot_id = context_data["policy_catalog_snapshot_id"]
        attachment_evidence = (
            self._attachment_evidence(approval_input, catalog_snapshot_id, source_document_ids)
            if case is not None
            else parsed_evidence or []
        )
        state = self._initial_state(
            approval_input,
            extracted_fields,
            attachment_evidence,
            limits,
            allowed_tools,
            trace_id=trace_id,
            policy_catalog_snapshot_id=catalog_snapshot_id,
            structured_data_snapshot_id=context_data["structured_data_snapshot_id"],
        )
        context = self._tool_context(
            state,
            catalog_snapshot_id=catalog_snapshot_id,
            structured_data_snapshot_id=context_data["structured_data_snapshot_id"],
            allowed_tools=allowed_tools,
        )
        return approval_input, state, context, extracted_fields

    def create_business_record(
        self,
        approval_input: ApprovalInput,
        extracted_fields: list[ExtractedField],
        *,
        idempotency_key: str,
    ) -> bool:
        inserted = self.store.create_approval(
            request_id=approval_input.request_id,
            thread_id=f"approval:{approval_input.request_id}",
            idempotency_key=idempotency_key,
            applicant=approval_input.applicant,
            application=approval_input.application,
            documents=approval_input.documents,
        )
        if inserted and extracted_fields:
            self.store.upsert_extracted_fields(approval_input.request_id, extracted_fields)
        return inserted

    def unstarted_request_for_retry(
        self, record: Mapping[str, object]
    ) -> tuple[ApprovalInput, str | None] | None:
        """Restore the original request without widening a fixture's tool scope."""

        request_id = str(record["request_id"])
        events = self.store.audit_events_after(request_id, limit=1)
        if not events or events[0]["event_type"] != "APPROVAL_CREATED":
            return None
        created = events[0]["payload"]
        try:
            approval_input = ApprovalInput.model_validate(
                {
                    "request_id": request_id,
                    "applicant": created["applicant"],
                    "application": record["application"],
                    "documents": created["documents"],
                }
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not approval_input.documents:
            return approval_input, None
        if not all(document.synthetic for document in approval_input.documents):
            return None  # A real invoice must be restored from its saved file.
        for case_id, case in self.cases.items():
            source = ApprovalInput.model_validate(case["input"])
            for suffix in (request_id, request_id[-12:].replace("_", "-")):
                expected = source.model_copy(
                    update={
                        "request_id": request_id,
                        "documents": [
                            document.model_copy(
                                update={"document_id": f"{document.document_id}-{suffix}"}
                            )
                            for document in source.documents
                        ],
                    }
                )
                if expected == approval_input:
                    return approval_input, case_id
        return None

    def _attachment_evidence(
        self,
        approval_input: ApprovalInput,
        catalog_snapshot_id: str,
        source_document_ids: dict[str, str],
    ) -> list[EvidenceItem]:
        requested_ids = {item.document_id for item in approval_input.documents}
        evidence = []
        for chunk in self.chunks:
            if chunk.source_kind != "ATTACHMENT":
                continue
            target_id = source_document_ids.get(chunk.document_id, chunk.document_id)
            if target_id not in requested_ids:
                continue
            evidence_id = hashlib.sha256(f"{chunk.chunk_id}:{target_id}".encode()).hexdigest()
            evidence.append(
                EvidenceItem(
                    evidence_id=evidence_id,
                    source_type=EvidenceSource.ATTACHMENT,
                    document_id=target_id,
                    version=chunk.version,
                    page=chunk.page_number,
                    section=" > ".join(chunk.title_path) or chunk.title,
                    excerpt=chunk.text,
                    catalog_snapshot_id=catalog_snapshot_id,
                )
            )
        return evidence

    @staticmethod
    def _initial_state(
        approval_input: ApprovalInput,
        extracted_fields: list[ExtractedField],
        attachment_evidence: list[EvidenceItem],
        limits: Mapping[str, int],
        allowed_tools: list[ToolName],
        *,
        trace_id: str | None = None,
        policy_catalog_snapshot_id: str | None = None,
        structured_data_snapshot_id: str | None = None,
    ) -> ApprovalState:
        return validate_state(
            {
                "request_id": approval_input.request_id,
                "trace_id": trace_id or f"TRACE-{approval_input.request_id}",
                "status": ApprovalStatus.RUNNING,
                "applicant": approval_input.applicant,
                "application": approval_input.application,
                "documents": approval_input.documents,
                "allowed_tools": allowed_tools,
                **(
                    {"policy_catalog_snapshot_id": policy_catalog_snapshot_id}
                    if policy_catalog_snapshot_id else {}
                ),
                **(
                    {"structured_data_snapshot_id": structured_data_snapshot_id}
                    if structured_data_snapshot_id else {}
                ),
                "extracted_fields": extracted_fields,
                "extraction_issues": [],
                "supervisor_decision": None,
                "supervisor_decisions": [],
                "supervisor_challenge_count": 0,
                "guardrail_decisions": [],
                "open_questions": [],
                "retrieval_plan": None,
                "evidence_review": None,
                "selected_tools": [],
                "query_variants": [],
                "tool_observations": [],
                "retrieved_candidates": [],
                "evidence": attachment_evidence,
                "evidence_metrics": EvidenceMetrics(),
                "agent_trajectory": [],
                "agent_step_count": 0,
                "agent_stop_reason": None,
                "rule_results": [],
                "risk_flags": [],
                "risk_level": None,
                "recommendation": None,
                "decision_reasons": [],
                "technical_retry_count": 0,
                "node_attempts": {},
                "retrieval_round_count": 0,
                "no_progress_rounds": 0,
                "query_rewrite_count": 0,
                "tokens_used": 0,
                "max_agent_steps": limits["max_agent_steps"],
                "max_retrieval_rounds": limits["max_retrieval_rounds"],
                "max_query_rewrites": limits["max_query_rewrites"],
                "token_budget": int(os.getenv("AGENT_TOKEN_BUDGET", "48000")),
                "deadline_at": datetime.now(UTC) + WORKFLOW_RUN_WINDOW,
                "pending_human_action": None,
                "final_decision": None,
            }
        )

    def _tool_context(
        self,
        state: ApprovalState,
        *,
        catalog_snapshot_id: str,
        structured_data_snapshot_id: str,
        allowed_tools: list[ToolName],
    ) -> ToolExecutionContext:
        application = state["application"]
        applicant = state["applicant"]
        active = active_catalog()
        policy_source_ids = (
            active["policy_source_snapshot_ids"]
            if catalog_snapshot_id == active["policy_catalog_snapshot_id"]
            else [catalog_snapshot_id]
        )
        structured_source_ids = (
            active["structured_source_snapshot_ids"]
            if structured_data_snapshot_id == active["structured_data_snapshot_id"]
            else [structured_data_snapshot_id]
        )
        allowed_documents = sorted(
            {
                policy["document_id"]
                for policy in self.manifest["policies"]
                if policy["catalog_snapshot_id"] in policy_source_ids
                and policy["published_status"] == "PUBLISHED"
                and (
                    "*" in policy["department_ids"]
                    or applicant.department_id in policy["department_ids"]
                )
                and application.expense_type.value in policy["expense_types"]
                and date.fromisoformat(policy["effective_from"]) <= application.occurred_on
                and (
                    policy["effective_to"] is None
                    or application.occurred_on <= date.fromisoformat(policy["effective_to"])
                )
            }
        )
        context = ToolExecutionContext(
            request_id=state["request_id"],
            requester_role="APPLICANT",
            department_id=applicant.department_id,
            allowed_department_ids=[applicant.department_id],
            allowed_document_ids=allowed_documents,
            allowed_tools=allowed_tools,
            allowed_structured_query_types=(
                [
                    *(["city_tier"] if application.expense_type == ExpenseType.LODGING and application.city else []),
                    "employee_department",
                    "budget_status",
                    *(
                        ["duplicate_invoice"]
                        if any(item.field == "invoice_number" and item.value is not None
                               for item in state.get("extracted_fields", []))
                        else []
                    ),
                ]
                if ToolName.STRUCTURED_LOOKUP in allowed_tools
                else []
            ),
            policy_catalog_snapshot_id=catalog_snapshot_id,
            structured_data_snapshot_id=structured_data_snapshot_id,
            policy_source_snapshot_ids=policy_source_ids,
            structured_source_snapshot_ids=structured_source_ids,
            policy_effective_at=application.occurred_on,
            structured_as_of=application.submitted_on,
            expense_type=application.expense_type,
            allowed_cities=[application.city] if application.city else [],
            allowed_employee_ids=[applicant.employee_id],
            allowed_invoice_numbers=[
                str(item.value)
                for item in state.get("extracted_fields", [])
                if item.field == "invoice_number" and item.value is not None
            ],
        )
        published_ids = {
            rule.rule_id
            for rule in self.store.policy_rules(
                expense_type=application.expense_type,
                effective_at=application.occurred_on,
                document_ids=set(allowed_documents),
            )
        }
        return context.model_copy(
            update={
                "applicable_rule_ids": sorted(
                    published_ids.intersection(required_rule_ids(state, context))
                ),
                "rule_scope_complete": True,
            }
        )

    def policies(self) -> list[dict[str, object]]:
        uploaded = [
            {**item["payload"], "created_at": item["created_at"], "source": "UPLOAD"}
            for item in self.store.policy_upload_events()
        ]
        declared = [{**item, "source": "MANIFEST"} for item in self.manifest["policies"]]
        return uploaded + declared

    def fixture_summaries(self) -> list[dict[str, str]]:
        titles = {
            "GC-A-TRANSPORT-COMMUTE-REJECT": "日常通勤费用不予报销",
            "GC-B-LODGING-MULTIHOP-PASS": "住宿标准与城市等级多跳核验",
            "GC-C-DINING-VERSION-CONFLICT-HUMAN": "餐饮制度版本冲突转人工",
        }
        return [
            {
                "case_id": case["case_id"],
                "title": titles[case["case_id"]],
                "expected_recommendation": case["expected_recommendation"],
            }
            for case in self.cases.values()
        ]

    def context_for_state(self, state: ApprovalState) -> ToolExecutionContext:
        allowed_tools = state.get("allowed_tools")
        if allowed_tools is None:
            # Checkpoints created before tool scope was saved: recover the original MVP scope.
            fixture = any(document.synthetic for document in state.get("documents", []))
            allowed_tools = [ToolName.POLICY_SEARCH]
            if not fixture:
                allowed_tools.extend([ToolName.CASE_SEARCH, ToolName.RULE_SEARCH])
            if state["application"].expense_type == ExpenseType.LODGING:
                allowed_tools.append(ToolName.STRUCTURED_LOOKUP)
        catalog_snapshot_id = state.get("policy_catalog_snapshot_id") or next(
            (
                item.catalog_snapshot_id
                for item in state.get("evidence", [])
                if item.catalog_snapshot_id
            ),
            self.manifest["golden_policy_catalog_snapshot_id"],
        )
        structured_snapshot_id = state.get("structured_data_snapshot_id") or next(
            (
                item.structured_data_snapshot_id
                for item in state.get("evidence", [])
                if item.structured_data_snapshot_id
            ),
            (
                self.manifest["golden_structured_data_snapshot_id"]
                if any(document.synthetic for document in state.get("documents", []))
                else DEMO_STRUCTURED_SNAPSHOT_ID
            ),
        )
        return self._tool_context(
            state,
            catalog_snapshot_id=catalog_snapshot_id,
            structured_data_snapshot_id=structured_snapshot_id,
            allowed_tools=allowed_tools,
        )


@contextmanager
def _runtime(
    context: ToolExecutionContext,
) -> Iterator[tuple[Any, ApprovalRuntime]]:
    store = PostgresStore()
    try:
        index = _shared_policy_index()
        registry = build_tool_registry(index, store)
        runtime = ApprovalRuntime(
            model=StructuredChatClient(),
            registry=registry,
            tool_context=context,
            database=store,
        )
        with open_persistent_approval_graph() as graph:
            yield graph, runtime
    finally:
        store.close()


def _shared_policy_index() -> HybridPolicyIndex:
    global _SHARED_INDEX
    with _RESOURCE_LOCK:
        if _SHARED_INDEX is None:
            _SHARED_INDEX = HybridPolicyIndex(
                uri=MILVUS_URI,
                chunks_path=CHUNKS_PATH,
                embedder=QwenEmbeddingClient(),
                reranker=QwenRerankerClient(),
                mode=POLICY_RETRIEVAL_MODE,
            )
        return _SHARED_INDEX


@contextmanager
def _request_slot(request_id: str) -> Iterator[bool]:
    with _RESOURCE_LOCK:
        acquired = request_id not in _ACTIVE_REQUESTS
        if acquired:
            _ACTIVE_REQUESTS.add(request_id)
    try:
        if not acquired:
            yield False
            return
        store = PostgresStore()
        try:
            with store.request_lock(request_id) as distributed:
                yield distributed
        finally:
            store.close()
    finally:
        if acquired:
            with _RESOURCE_LOCK:
                _ACTIVE_REQUESTS.discard(request_id)


def _node_event_payload(
    node: str, update: object, state: Mapping[str, object]
) -> dict[str, object]:
    delta = update if isinstance(update, Mapping) else {}
    steps = delta.get("agent_trajectory", [])
    observations = delta.get("tool_observations", [])
    evidence = delta.get("evidence", [])
    return {
        "node": node,
        "trace_id": state.get("trace_id"),
        "status": jsonable(state.get("status")),
        "recommendation": jsonable(state.get("recommendation")),
        "risk_level": jsonable(state.get("risk_level")),
        "agent_step_count": max(
            int(state.get("agent_step_count", 0)),
            int(steps[-1].step_index) if steps else 0,
        ),
        "agent_step": jsonable(steps[-1]) if steps else None,
        "tool_observations": [
            {
                "call_id": item.call_id,
                "tool_name": item.tool_name.value,
                "latency_ms": item.latency_ms,
                "error": item.error,
                "is_degraded": item.is_degraded,
                "source_ids": item.source_ids,
            }
            for item in observations
        ],
        "evidence_ids": [item.evidence_id for item in evidence],
    }


def _stream_and_audit(
    graph: Any,
    runtime: ApprovalRuntime,
    request_id: str,
    graph_input: ApprovalState | Command | None,
) -> None:
    config = approval_config(request_id)
    for packet in graph.stream(
        graph_input,
        config=config,
        context=runtime,
        stream_mode="updates",
    ):
        if not isinstance(packet, Mapping):
            continue
        snapshot = graph.get_state(config)
        checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id", "pending")
        for node, update in packet.items():
            if node == "__interrupt__":
                continue
            runtime.database.record_event(
                request_id=request_id,
                event_type="WORKFLOW_NODE_COMPLETED",
                actor="SYSTEM",
                payload=_node_event_payload(node, update, snapshot.values),
                dedupe_key=f"workflow:{request_id}:{checkpoint_id}:{node}",
            )


def _write_memory_best_effort(graph: Any, runtime: ApprovalRuntime, request_id: str) -> None:
    snapshot = graph.get_state(approval_config(request_id))
    if not snapshot.values or snapshot.next:
        return
    try:
        write_memory(validate_state(snapshot.values), runtime.database)
    except Exception as exc:  # noqa: BLE001 - a completed approval must stay completed
        try:
            runtime.database.record_event(
                request_id=request_id,
                event_type="CASE_WRITE_FAILED",
                actor="SYSTEM",
                payload={"error": type(exc).__name__},
                dedupe_key=f"case-write-failed:{request_id}:{type(exc).__name__}",
            )
        except Exception:  # noqa: BLE001, S110 - approval is already finalized
            pass


def run_approval_background(
    state: ApprovalState,
    context: ToolExecutionContext,
    *,
    slot_acquired: bool = False,
    renew_deadline: bool = False,
) -> None:
    request_id = state["request_id"]
    try:
        with (nullcontext(True) if slot_acquired else _request_slot(request_id)) as acquired:
            if not acquired:
                return
            with _runtime(context) as (graph, runtime):
                snapshot = graph.get_state(approval_config(request_id))
                if snapshot.values:
                    if snapshot.next and not any(task.interrupts for task in snapshot.tasks):
                        if renew_deadline:
                            graph.update_state(
                                approval_config(request_id),
                                {"deadline_at": datetime.now(UTC) + WORKFLOW_RUN_WINDOW},
                            )
                        _stream_and_audit(graph, runtime, request_id, None)
                    _write_memory_best_effort(graph, runtime, request_id)
                    return
                _stream_and_audit(graph, runtime, request_id, state)
                _write_memory_best_effort(graph, runtime, request_id)
    except Exception as exc:  # noqa: BLE001 - background boundary must leave an audit trail
        store = PostgresStore()
        try:
            failure_hash = hashlib.sha256(f"{type(exc).__name__}:{exc}".encode()).hexdigest()[:12]
            store.mark_workflow_failed(
                request_id,
                error=type(exc).__name__,
                message=str(exc)[:300],
                failure_key=failure_hash,
                trace_id=state["trace_id"],
            )
        finally:
            store.close()


def run_invoice_approval_background(
    approval_input: ApprovalInput,
    invoice_path: Path,
    trace_id: str,
) -> None:
    """Parse the saved invoice before constructing the Agent's initial state."""

    request_id = approval_input.request_id
    store = PostgresStore()
    try:
        with _request_slot(request_id) as acquired:
            if not acquired:
                return
            document: Document = approval_input.documents[0]
            fields, evidence = parse_uploaded_invoice(invoice_path, document)
            store.upsert_extracted_fields(request_id, fields)
            field_count = sum(item.status == ExtractionStatus.PRESENT for item in fields)
            store.record_event(
                request_id=request_id,
                event_type="DOCUMENT_PARSED",
                actor="SYSTEM",
                payload={
                    "document_id": document.document_id,
                    "field_count": field_count,
                    "evidence_count": len(evidence),
                },
                dedupe_key=f"document-parsed:{document.document_id}:{field_count}:{len(evidence)}",
            )
            _, state, context, _ = ApprovalService(store).prepare(
                approval_input,
                None,
                None,
                trace_id=trace_id,
                parsed_fields=fields,
                parsed_evidence=evidence,
            )
            run_approval_background(state, context, slot_acquired=True)
    except Exception as exc:  # noqa: BLE001 - failed invoice startup must be visible
        failure_hash = hashlib.sha256(f"{type(exc).__name__}:{exc}".encode()).hexdigest()[:12]
        store.mark_workflow_failed(
            request_id,
            error=type(exc).__name__,
            message=str(exc)[:300],
            failure_key=failure_hash,
            trace_id=trace_id,
        )
        return
    finally:
        store.close()


def resume_review_background(
    request_id: str,
    context: ToolExecutionContext,
    submission: HumanReviewSubmission,
) -> None:
    try:
        with _request_slot(request_id) as acquired:
            if not acquired:
                return
            with _runtime(context) as (graph, runtime):
                snapshot = graph.get_state(approval_config(request_id))
                if not snapshot.values:
                    raise LookupError(f"unknown approval thread: {request_id}")
                if any(task.interrupts for task in snapshot.tasks):
                    _stream_and_audit(
                        graph,
                        runtime,
                        request_id,
                        Command(resume=submission.model_dump(mode="json")),
                    )
                elif snapshot.next:
                    _stream_and_audit(graph, runtime, request_id, None)
                _write_memory_best_effort(graph, runtime, request_id)
    except Exception as exc:  # noqa: BLE001 - preserve the failed resume for operators
        store = PostgresStore()
        try:
            store.record_event(
                request_id=request_id,
                event_type="HUMAN_REVIEW_RESUME_FAILED",
                actor=submission.operator_id,
                payload={"error": type(exc).__name__, "message": str(exc)[:300]},
                dedupe_key=f"human-resume-failed:{request_id}:{submission.idempotency_key}",
            )
        finally:
            store.close()


def checkpoint_state(request_id: str) -> dict[str, object] | None:
    with open_persistent_approval_graph() as graph:
        snapshot = graph.get_state(approval_config(request_id))
        if not snapshot.values:
            return None
        interrupted = any(task.interrupts for task in snapshot.tasks)
        return {
            "state": jsonable(snapshot.values),
            "next_nodes": list(snapshot.next),
            "interrupted": interrupted,
            "allowed_actions": (
                [item.value for item in allowed_human_actions(validate_state(snapshot.values))]
                if interrupted
                else []
            ),
        }
