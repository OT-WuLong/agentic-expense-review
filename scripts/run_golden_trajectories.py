"""Run the three frozen golden cases through the live P08 approval workflow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.contracts import SupervisorAction, ToolName
from app.agents.model import AGENT_MODEL, StructuredChatClient
from app.agents.supervisor import MAX_SUPERVISOR_CHALLENGES
from app.database import PostgresStore
from app.graph.approval import ApprovalRuntime, build_approval_graph
from app.graph.state import ApprovalState, validate_state
from app.ingestion.models import DocumentChunk
from app.models import (
    ApprovalInput,
    ApprovalStatus,
    EvidenceItem,
    EvidenceMetrics,
    EvidenceSource,
)
from app.retrieval import HybridPolicyIndex, QwenEmbeddingClient, QwenRerankerClient
from app.tools import ToolExecutionContext, build_tool_registry


def _json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (datetime, Decimal, Enum)):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _compact(value: str) -> str:
    return "".join(value.split())


def _input_attachment_evidence(
    chunks: list[DocumentChunk], approval_input: ApprovalInput, catalog_snapshot_id: str
) -> list[EvidenceItem]:
    document_ids = {item.document_id for item in approval_input.documents}
    return [
        EvidenceItem(
            evidence_id=chunk.chunk_id,
            source_type=EvidenceSource.ATTACHMENT,
            document_id=chunk.document_id,
            version=chunk.version,
            page=chunk.page_number,
            section=" > ".join(chunk.title_path) or chunk.title,
            excerpt=chunk.text,
            catalog_snapshot_id=catalog_snapshot_id,
        )
        for chunk in chunks
        if chunk.source_kind == "ATTACHMENT" and chunk.document_id in document_ids
    ]


def _initial_state(
    case: dict[str, Any], chunks: list[DocumentChunk], database: PostgresStore
) -> ApprovalState:
    approval_input = ApprovalInput.model_validate(case["input"])
    limits = case["hard_limits"]
    return validate_state(
        {
            "request_id": approval_input.request_id,
            "trace_id": f"TRACE-{approval_input.request_id}",
            "status": ApprovalStatus.RUNNING,
            "applicant": approval_input.applicant,
            "application": approval_input.application,
            "documents": approval_input.documents,
            "extracted_fields": database.extracted_fields(approval_input.request_id),
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
            "evidence": _input_attachment_evidence(
                chunks,
                approval_input,
                case["fixture_context"]["policy_catalog_snapshot_id"],
            ),
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
            "token_budget": 20_000,
            "deadline_at": datetime.now(UTC) + timedelta(minutes=7),
            "pending_human_action": None,
            "final_decision": None,
        }
    )


def _runtime_context(
    case: dict[str, Any], state: ApprovalState, chunks: list[DocumentChunk]
) -> ToolExecutionContext:
    application = state["application"]
    applicant = state["applicant"]
    catalog_snapshot_id = case["fixture_context"]["policy_catalog_snapshot_id"]
    allowed_documents = sorted(
        {
            chunk.document_id
            for chunk in chunks
            if chunk.source_kind == "POLICY"
            and chunk.catalog_snapshot_id == catalog_snapshot_id
            and chunk.published_status == "PUBLISHED"
        }
    )
    allowed_tools = [ToolName(item) for item in case["allowed_tools"]]
    return ToolExecutionContext(
        requester_role="APPLICANT",
        department_id=applicant.department_id,
        allowed_department_ids=[applicant.department_id],
        allowed_document_ids=allowed_documents,
        allowed_tools=allowed_tools,
        allowed_structured_query_types=(
            ["city_tier", "employee_department", "budget_status", "duplicate_invoice"]
            if ToolName.STRUCTURED_LOOKUP in allowed_tools
            else []
        ),
        policy_catalog_snapshot_id=catalog_snapshot_id,
        structured_data_snapshot_id=case["fixture_context"]["structured_data_snapshot_id"],
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


def _matches_gold(gold: dict[str, Any], actual: EvidenceItem) -> bool:
    if gold["source_type"] == "STRUCTURED_RECORD":
        return (
            actual.source_type == EvidenceSource.STRUCTURED_RECORD
            and actual.fixture_id == gold.get("fixture_id")
            and actual.query_type == gold.get("query_type")
        )
    if actual.document_id != gold.get("document_id") or actual.page != gold.get("page"):
        return False
    if gold["source_type"] == "ATTACHMENT":
        return actual.source_type == EvidenceSource.ATTACHMENT
    expected = _compact(gold.get("excerpt", ""))
    return actual.source_type in {
        EvidenceSource.POLICY_DOCUMENT,
        EvidenceSource.DOCUMENT_CONTEXT,
    } and expected in _compact(actual.excerpt or "")


def _evaluate(
    case: dict[str, Any],
    state: ApprovalState,
    trajectory_policy: dict[str, Any],
) -> dict[str, Any]:
    calls = [call for step in state["agent_trajectory"] for call in step.tool_calls]
    critical = {item["evidence_id"]: item for item in case["critical_evidence"]}
    matched_gold_ids = {
        gold_id
        for gold_id, gold in critical.items()
        if any(_matches_gold(gold, actual) for actual in state["evidence"])
    }
    group_matches = {
        group["group_id"]: bool(set(group["any_of"]) & matched_gold_ids)
        for group in case["acceptable_evidence_groups"]
    }
    groups = {item["group_id"]: set(item["any_of"]) for item in case["acceptable_evidence_groups"]}
    call_gold_ids: dict[str, set[str]] = {}
    for observation in state["tool_observations"]:
        if observation.error is not None or not observation.items or not observation.call_id:
            continue
        call_gold_ids.setdefault(observation.call_id, set()).update(
            gold_id
            for gold_id, gold in critical.items()
            if any(_matches_gold(gold, item) for item in observation.items)
        )
    calls_by_id = {call.call_id: call for call in calls if call.call_id}
    required_capabilities = case["required_tool_capabilities"]
    candidate_call_ids: list[list[str]] = []
    for required in required_capabilities:
        candidate_call_ids.append(
            [
                call_id
                for call_id, call in calls_by_id.items()
                if call_id in call_gold_ids
                and call.tool_name.value in required["one_of"]
                and (
                    not required.get("query_type")
                    or call.query_type == required["query_type"]
                )
                and all(
                    bool(groups.get(group_id, set()) & call_gold_ids[call_id])
                    for group_id in required["required_evidence_group_ids"]
                )
            ]
        )

    capability_order = sorted(
        range(len(required_capabilities)), key=lambda index: len(candidate_call_ids[index])
    )
    call_assignment: dict[str, int] = {}

    def assign_distinct_call(capability_index: int, seen: set[str]) -> bool:
        for call_id in candidate_call_ids[capability_index]:
            if call_id in seen:
                continue
            seen.add(call_id)
            previous = call_assignment.get(call_id)
            if previous is None or assign_distinct_call(previous, seen):
                call_assignment[call_id] = capability_index
                return True
        return False

    for capability_index in capability_order:
        assign_distinct_call(capability_index, set())
    assignment = {
        capability_index: call_id
        for call_id, capability_index in call_assignment.items()
    }
    capabilities = []
    for index, required in enumerate(required_capabilities):
        matched_call_id = assignment.get(index)
        matched_call = calls_by_id.get(matched_call_id)
        capabilities.append(
            {
                "capability_label": required["purpose"],
                "tool_options": required["one_of"],
                "query_type": required.get("query_type") or None,
                "candidate_call_ids": candidate_call_ids[index],
                "matched_call_ids": [matched_call_id] if matched_call_id else [],
                "actual_purposes": [matched_call.purpose] if matched_call else [],
                "matched": matched_call_id is not None,
            }
        )

    required_group_ids = set(case["trajectory_contract"]["required_evidence_group_ids"])
    evidence_groups = [
        {
            "group_id": group["group_id"],
            "matched": group_matches[group["group_id"]],
        }
        for group in case["acceptable_evidence_groups"]
        if group["group_id"] in required_group_ids
    ]
    stop_ok = state.get("agent_stop_reason") in case["acceptable_stop_reasons"]
    retrieval_passed = (
        state["status"] != ApprovalStatus.SYSTEM_ERROR
        and stop_ok
        and all(item["matched"] for item in capabilities)
        and all(item["matched"] for item in evidence_groups)
    )
    actual_rules = {item.rule_id: item.model_dump(mode="json") for item in state["rule_results"]}
    rule_checks = []
    compared_fields = (
        "outcome",
        "reason_code",
        "computed_limit",
        "actual_amount",
        "computed_value",
        "unit",
    )
    for expected in case["expected_rule_results"]:
        actual = actual_rules.get(expected["rule_id"])
        matched = actual is not None and all(
            str(actual.get(field)) == str(expected.get(field))
            for field in compared_fields
            if field in expected
        )
        rule_checks.append({"rule_id": expected["rule_id"], "matched": matched})
    recommendation = state.get("recommendation")
    recommendation_matched = (
        recommendation is not None and recommendation.value == case["expected_recommendation"]
    )
    risk_flags_matched = set(case["expected_risk_flags"]) <= set(state["risk_flags"])
    supervisor_records = state.get("supervisor_decisions", [])
    challenge_records = [
        item
        for item in supervisor_records
        if item.action == SupervisorAction.CHALLENGE_AND_RETRIEVE
    ]
    supervisor_steps = [
        item
        for item in state["agent_trajectory"]
        if item.agent.value == "SUPERVISOR"
    ]
    guardrail_records = state.get("guardrail_decisions", [])
    challenge_limit = trajectory_policy["max_supervisor_challenges"]
    supervisor_invariants = {
        "decision_sequence_contiguous": [item.sequence for item in supervisor_records]
        == list(range(1, len(supervisor_records) + 1)),
        "challenge_limit_matches_runtime": challenge_limit
        == MAX_SUPERVISOR_CHALLENGES,
        "challenge_within_limit": len(challenge_records) <= challenge_limit,
        "challenge_targets_questions": all(
            item.challenged_question_ids and item.new_goal for item in challenge_records
        ),
        "retrieval_goals_changed": all(
            item.previous_goal is None
            or _compact(item.previous_goal) != _compact(item.new_goal or "")
            for item in supervisor_records
            if item.action
            in {
                SupervisorAction.RETRIEVE,
                SupervisorAction.CHALLENGE_AND_RETRIEVE,
            }
        ),
        "model_steps_match": sum(item.used_model for item in supervisor_records)
        == len(supervisor_steps),
        "guardrail_sequence_contiguous": [item.sequence for item in guardrail_records]
        == list(range(1, len(guardrail_records) + 1)),
        "guardrail_records_valid": all(
            (
                item.phase == "PRE_TOOL"
                and item.action == "VETO"
                and item.subject_role.value == "RETRIEVAL"
                and item.related_supervisor_sequence is not None
                and any(
                    decision.sequence == item.related_supervisor_sequence
                    for decision in supervisor_records
                )
            )
            or (
                item.phase == "POST_REVIEW"
                and item.action == "STOP"
                and item.subject_role.value == "EVIDENCE_REVIEWER"
                and item.related_supervisor_sequence is None
            )
            for item in guardrail_records
        ),
    }
    passed = (
        retrieval_passed
        and all(item["matched"] for item in rule_checks)
        and recommendation_matched
        and risk_flags_matched
    )
    return {
        "passed": passed,
        "retrieval_passed": retrieval_passed,
        "stop_reason_accepted": stop_ok,
        "required_capabilities": capabilities,
        "required_evidence_groups": evidence_groups,
        "matched_gold_evidence_ids": sorted(matched_gold_ids),
        "rule_results": rule_checks,
        "recommendation_matched": recommendation_matched,
        "risk_flags_matched": risk_flags_matched,
        "supervisor_invariants": supervisor_invariants,
    }


def _git_state() -> dict[str, object]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    return {"commit": commit, "dirty": dirty}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases", type=Path, default=ROOT / "data/fixtures/golden_cases.json"
    )
    parser.add_argument(
        "--chunks", type=Path, default=ROOT / "data/fixtures/p04_chunks.jsonl"
    )
    parser.add_argument("--milvus-uri", default=os.getenv("MILVUS_URI", "http://localhost:19530"))
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evals/reports/golden_trajectories.latest.json",
    )
    parser.add_argument(
        "--last-pass-output",
        type=Path,
        default=ROOT / "evals/reports/golden_trajectories.last_pass.json",
    )
    args = parser.parse_args()

    dataset = json.loads(args.cases.read_text(encoding="utf-8"))
    chunks = [
        DocumentChunk.model_validate_json(line)
        for line in args.chunks.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    policy_index = HybridPolicyIndex(
        uri=args.milvus_uri,
        chunks_path=args.chunks,
        embedder=QwenEmbeddingClient(),
        reranker=QwenRerankerClient(),
        mode=os.getenv("POLICY_RETRIEVAL_MODE", "dense"),
    )
    database = PostgresStore()
    database.ping()
    registry = build_tool_registry(policy_index, database)
    model = StructuredChatClient()
    graph = build_approval_graph(InMemorySaver())
    results: list[dict[str, Any]] = []
    try:
        selected_cases = [
            case
            for case in dataset["cases"]
            if not args.case_id or case["case_id"] in set(args.case_id)
        ]
        for case in selected_cases:
            state = _initial_state(case, chunks, database)
            input_evidence_ids = [item.evidence_id for item in state["evidence"]]
            context = _runtime_context(case, state, chunks)
            started = time.perf_counter()
            output = graph.invoke(
                state,
                context=ApprovalRuntime(
                    model,
                    registry,
                    context,
                    database,
                    persist_workflow=False,
                ),
                config={
                    "configurable": {"thread_id": f"golden:{case['case_id']}"},
                    "recursion_limit": 20,
                },
            )
            was_interrupted = bool(output.pop("__interrupt__", []))
            final_state = validate_state(output)
            results.append(
                {
                    "case_id": case["case_id"],
                    "scenario": case["scenario"],
                    "status": final_state["status"].value,
                    "stop_reason": final_state.get("agent_stop_reason"),
                    "agent_steps": final_state["agent_step_count"],
                    "retrieval_rounds": final_state["retrieval_round_count"],
                    "query_rewrites": final_state["query_rewrite_count"],
                    "tokens_used": final_state["tokens_used"],
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "trajectory": final_state["agent_trajectory"],
                    "supervisor_decisions": final_state["supervisor_decisions"],
                    "supervisor_challenge_count": final_state[
                        "supervisor_challenge_count"
                    ],
                    "guardrail_decisions": final_state["guardrail_decisions"],
                    "tool_observations": final_state["tool_observations"],
                    "input_evidence_ids": input_evidence_ids,
                    "retrieved_evidence_ids": [
                        item.evidence_id for item in final_state["retrieved_candidates"]
                    ],
                    "rule_evidence_ids": [
                        item.evidence_id
                        for item in final_state["evidence"]
                        if item.evidence_id not in input_evidence_ids
                        and item.evidence_id
                        not in {
                            candidate.evidence_id
                            for candidate in final_state["retrieved_candidates"]
                        }
                    ],
                    "evidence_review": final_state.get("evidence_review"),
                    "rule_results": final_state["rule_results"],
                    "risk_flags": final_state["risk_flags"],
                    "risk_level": (
                        final_state.get("risk_level").value
                        if final_state.get("risk_level")
                        else None
                    ),
                    "recommendation": (
                        final_state.get("recommendation").value
                        if final_state.get("recommendation")
                        else None
                    ),
                    "human_interrupted": was_interrupted,
                    "decision_reasons": final_state.get("decision_reasons", []),
                    "evidence_metrics_status": "PARTIALLY_MEASURED",
                    "unmeasured_metrics": ["retrieval_margin"],
                    "evidence_metrics": final_state["evidence_metrics"],
                    "evaluation": _evaluate(
                        case, final_state, dataset["trajectory_policy"]
                    ),
                }
            )
    finally:
        policy_index.close()
        database.close()

    if args.case_id and args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        merged = {item["case_id"]: item for item in previous.get("cases", [])}
        merged.update({item["case_id"]: item for item in results})
        case_order = [item["case_id"] for item in dataset["cases"]]
        results = [merged[case_id] for case_id in case_order if case_id in merged]

    report = {
        "schema": "golden-approval-trajectories/v5",
        "generated_at": datetime.now(UTC).isoformat(),
        "git": _git_state(),
        "models": {
            "agent": AGENT_MODEL,
            "embedding": "qwen3.7-text-embedding-flash",
        },
        "graph": graph.get_graph().draw_mermaid(),
        "golden_dataset": {
            "dataset_id": dataset["dataset_id"],
            "dataset_version": dataset["dataset_version"],
        },
        "gold_labels_visible_to_agents": False,
        "upstream_extraction_source": "P04_DATABASE_FIXTURE",
        "run_scope": {
            "mode": "TARGETED" if args.case_id else "FULL",
            "executed_case_ids": [case["case_id"] for case in selected_cases],
            "merged_previous_results": bool(args.case_id and args.output.exists()),
        },
        "summary": {
            "cases": len(results),
            "passed": sum(item["evaluation"]["passed"] for item in results),
            "total_tokens": sum(item["tokens_used"] for item in results),
            "total_latency_ms": sum(item["latency_ms"] for item in results),
            "evidence_metrics_status": "PARTIALLY_MEASURED",
            "unmeasured_metrics": ["retrieval_margin"],
        },
        "cases": results,
    }
    full_run = not args.case_id
    complete_pass = (
        full_run
        and len(results) == len(dataset["cases"])
        and report["summary"]["passed"] == len(dataset["cases"])
    )
    report["run_outcome"] = (
        "PASS" if complete_pass else "PARTIAL" if not full_run else "FAIL"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        report, ensure_ascii=False, indent=2, default=_json_default
    ) + "\n"
    args.output.write_text(serialized, encoding="utf-8")
    if complete_pass:
        args.last_pass_output.parent.mkdir(parents=True, exist_ok=True)
        args.last_pass_output.write_text(serialized, encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": report["summary"]["passed"],
                "cases": report["summary"]["cases"],
                "tokens": report["summary"]["total_tokens"],
                "run_outcome": report["run_outcome"],
                "latest_report": str(args.output),
                "last_pass_updated": complete_pass,
                "results": [
                    {
                        "case_id": item["case_id"],
                        "status": item["status"],
                        "stop_reason": item["stop_reason"],
                        "passed": item["evaluation"]["passed"],
                    }
                    for item in results
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
