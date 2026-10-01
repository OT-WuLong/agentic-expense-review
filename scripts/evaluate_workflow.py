"""Evaluate non-golden approval and trajectory samples with the live workflow."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.contracts import (
    AgentAction,
    AgentRole,
    AgentStep,
    RetrievalAction,
    SubQuestion,
    ToolCall,
    ToolName,
)
from app.agents.evidence_reviewer import EVIDENCE_REVIEW_PROMPT_VERSION
from app.agents.model import StructuredChatClient
from app.agents.retrieval import RETRIEVAL_PROMPT_VERSION, plan_retrieval
from app.agents.supervisor import SUPERVISOR_PROMPT_VERSION
from app.approval_service import ApprovalService, active_catalog
from app.database import PostgresStore
from app.graph.approval import ApprovalRuntime, build_approval_graph
from app.graph.routing import route_risk
from app.graph.state import ApprovalState, validate_state
from app.ingestion.models import DocumentChunk, ParsedBlock
from app.ingestion.pipeline import extract_document_fields
from app.models import ApprovalInput, ApprovalStatus, EvidenceItem, ExtractedField
from app.retrieval import HybridPolicyIndex, QwenEmbeddingClient, QwenRerankerClient
from app.rules import evaluate_rules
from app.tools import build_tool_registry
from app.tools.registry import ToolExecutionContext, ToolRegistry
from scripts.run_golden_trajectories import (
    _input_attachment_evidence,
    _matches_gold,
    _runtime_context,
)


def _jsonl(path: Path, split: str) -> list[dict]:
    return [
        row
        for line in path.read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line))["split"] == split
    ]


def _approval(sample: dict) -> ApprovalInput:
    source = sample["input"]
    application = source["application"]
    return ApprovalInput.model_validate(
        {
            "request_id": application["request_id"],
            "applicant": source["applicant"],
            "application": {
                key: value for key, value in application.items() if key != "request_id"
            },
            "documents": source["documents"],
        }
    )


def _fixture_fields(approval: ApprovalInput, chunks: list[DocumentChunk]) -> list[ExtractedField]:
    """Run the P04 extractor on parsed attachment text, never on expected fields."""

    fields: list[ExtractedField] = []
    for document in approval.documents:
        blocks = [
            ParsedBlock(
                page_idx=chunk.page_idx,
                block_index=chunk.block_indices[0],
                kind="text",
                text=chunk.text,
                bbox=chunk.bboxes[0] if chunk.bboxes else None,
            )
            for chunk in chunks
            if chunk.source_kind == "ATTACHMENT" and chunk.document_id == document.document_id
        ]
        for item in extract_document_fields(blocks, document.model_dump(mode="json")):
            fields.append(
                ExtractedField.model_validate(
                    item.model_dump(
                        include={"field", "status", "value", "raw_value", "document_id", "page"}
                    )
                )
            )
    return fields


def _baseline_run(
    variant: str,
    state: ApprovalState,
    model: StructuredChatClient,
    registry: ToolRegistry,
    context: ToolExecutionContext,
    database: PostgresStore,
) -> ApprovalState:
    """One bounded retrieval pass, then the same deterministic rules as the graph."""

    application = state["application"]
    if variant == "single_retrieval_agent":
        plan, tokens, planner_latency_ms = plan_retrieval(model, state, context.allowed_tools)
        calls = plan.tool_calls
        questions = plan.sub_questions
    else:
        expense = application.expense_type.value
        questions = [SubQuestion(question_id="Q1", text="适用制度与业务证据是什么？")]
        queries = [f"{expense} {application.description[:350]} 报销制度"]
        if variant == "fixed_rewrite":
            queries.append(f"{expense} 适用范围 限额 票据 禁止 例外 条款")
        calls = [
            ToolCall(
                tool_name=ToolName.POLICY_SEARCH,
                query=query,
                purpose="检索适用制度",
                filters={
                    "expense_type": expense,
                    "effective_at": application.occurred_on.isoformat(),
                    "top_k": 5,
                },
                sub_question_id="Q1",
                call_id=f"R01-C{index:02d}",
            )
            for index, query in enumerate(queries, start=1)
        ]
        tokens = 0
        planner_latency_ms = 0
    state["open_questions"] = questions
    state["selected_tools"] = calls
    state["retrieval_round_count"] = 1 if calls else 0
    state["query_rewrite_count"] = int(variant == "fixed_rewrite")
    state["tokens_used"] = tokens
    if calls or variant == "single_retrieval_agent":
        state["agent_step_count"] = 1
        state["agent_trajectory"] = [
            AgentStep(
                step_index=1,
                agent=AgentRole.RETRIEVAL,
                action=AgentAction(
                    plan.action.value
                    if variant == "single_retrieval_agent"
                    else RetrievalAction.RETRIEVE.value
                ),
                thought_summary=("固定检索策略" if variant.startswith("fixed_") else plan.reason),
                tool_calls=calls,
                latency_ms=planner_latency_ms,
                tokens_used=tokens,
            )
        ]
    seen = {item.evidence_id for item in state["evidence"]}
    observations = []
    for call in calls:
        observation = registry.execute(call, context, known_evidence_ids=seen)
        observations.append(observation)
        for item in observation.items:
            if item.evidence_id not in seen:
                state["evidence"].append(item)
                state["retrieved_candidates"].append(item)
                seen.add(item.evidence_id)
    state["tool_observations"] = observations
    rule_output = evaluate_rules(state, database, context)
    state["rule_results"] = rule_output.results
    state["evidence"].extend(rule_output.new_evidence)
    state["risk_flags"] = rule_output.risk_flags
    state["evidence_metrics"] = rule_output.metrics
    state.update(route_risk(state))
    state["agent_stop_reason"] = (
        "ALL_NECESSARY_EVIDENCE_COVERED"
        if state["status"] in {ApprovalStatus.COMPLETED, ApprovalStatus.BUSINESS_REJECTED}
        else "EVIDENCE_ESCALATION"
    )
    return validate_state(state)


def _matched_ids(items: list[EvidenceItem], reference: dict[str, dict]) -> set[str]:
    return {
        evidence_id
        for evidence_id, gold in reference.items()
        if any(_matches_gold(gold, item) for item in items)
    }


def _capabilities_met(trajectory: dict, state: dict) -> bool:
    calls = {
        call.call_id: call
        for step in state["agent_trajectory"]
        for call in step.tool_calls
        if call.call_id
    }
    observations = {
        item.call_id: item
        for item in state["tool_observations"]
        if item.call_id and item.error is None and item.items
    }
    remaining = set(calls) & set(observations)
    for required in trajectory["required_tool_capabilities"]:
        match = next(
            (
                call_id
                for call_id in sorted(remaining)
                if calls[call_id].tool_name.value == required["selected_tool_name"]
                and (
                    not required.get("query_type")
                    or calls[call_id].query_type == required["query_type"]
                )
            ),
            None,
        )
        if match is None:
            return False
        remaining.remove(match)
    return True


_STOP_EQUIVALENTS = {
    "EVIDENCE_SUFFICIENT": {"ALL_NECESSARY_EVIDENCE_COVERED"},
    "CLEAR_RULE_FAILURE": {"ALL_NECESSARY_EVIDENCE_COVERED"},
    "MATERIALS_UNAVAILABLE": {"REQUEST_DOCUMENTS"},
    "MANDATORY_HUMAN_EXCEPTION": {
        "EVIDENCE_ESCALATION",
        "UNRESOLVED_POLICY_VERSION_CONFLICT",
    },
}


def _stop_accepted(sample: dict, actual: str | None) -> bool:
    return actual is not None and any(
        actual == expected or actual in _STOP_EQUIVALENTS.get(expected, set())
        for expected in sample["acceptable_stop_reasons"]
    )


def _metric(
    metric_id: str,
    numerator: float,
    denominator: int,
    split: str,
    *,
    direction: str = "higher",
    unit: str = "ratio",
) -> dict:
    return {
        "metric_id": metric_id,
        "status": "MEASURED" if denominator else "NOT_APPLICABLE",
        "numerator": numerator,
        "denominator": denominator,
        "support": denominator,
        "value": numerator / denominator if denominator else None,
        "unit": unit,
        "direction": direction,
        "split": split,
        "slice": "overall",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "validation", "test"), default="validation")
    parser.add_argument("--dataset-dir", type=Path, default=ROOT / "evals/datasets")
    parser.add_argument("--attachment-chunks", type=Path)
    parser.add_argument("--attachment-evidence", type=Path)
    parser.add_argument("--sample-id", action="append", help="只运行指定的审批样本，可重复")
    parser.add_argument(
        "--active-catalog", action="store_true",
        help="按网页新申请的单公司目录评测，保留样本原始来源快照不变",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--variant",
        choices=(
            "fixed_rag",
            "fixed_rewrite",
            "single_retrieval_agent",
            "no_supervisor",
            "multi_agent",
        ),
        default="multi_agent",
    )
    args = parser.parse_args()
    approvals = [
        row for row in _jsonl(args.dataset_dir / "approvals.jsonl", args.split)
        if not args.sample_id or row["sample_id"] in args.sample_id
    ]
    selected_cases = {row["case_id"] for row in approvals}
    trajectories = {
        row["case_id"]: row
        for row in _jsonl(args.dataset_dir / "agent_trajectories.jsonl", args.split)
        if row["case_id"] in selected_cases
    }
    if not approvals or selected_cases != set(trajectories):
        raise RuntimeError("approval and trajectory cases must match in this split")
    reference = {
        row["evidence_id"]: row
        for row in json.loads(
            (ROOT / "evals/datasets/reference_evidence.json").read_text(encoding="utf-8")
        )["items"]
    }
    if args.attachment_evidence:
        reference.update(
            (row["evidence_id"], row)
            for row in json.loads(args.attachment_evidence.read_text(encoding="utf-8"))["items"]
        )
    chunks_path = ROOT / "data/fixtures/p04_chunks.jsonl"
    chunks = [
        DocumentChunk.model_validate_json(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.attachment_chunks:
        chunks.extend(
            DocumentChunk.model_validate_json(line)
            for line in args.attachment_chunks.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    database = PostgresStore()
    index = HybridPolicyIndex(
        uri=os.getenv("MILVUS_URI", "http://localhost:19530"),
        chunks_path=chunks_path,
        embedder=QwenEmbeddingClient(),
        reranker=QwenRerankerClient(),
        mode=os.getenv("POLICY_RETRIEVAL_MODE", "dense"),
    )
    registry = build_tool_registry(index, database)
    graph = build_approval_graph(InMemorySaver())
    model = StructuredChatClient()
    rows: list[dict] = []
    try:
        database.ping()
        for sample in approvals:
            trajectory = trajectories[sample["case_id"]]
            approval = _approval(sample)
            fields = _fixture_fields(approval, chunks)
            catalog = active_catalog() if args.active_catalog else None
            policy_snapshot = (
                catalog["policy_catalog_snapshot_id"]
                if catalog else sample["policy_catalog_snapshot_id"]
            )
            structured_snapshot = sample.get(
                "structured_data_snapshot_id", "STRUCTURED-DATA-REF-1.2.0"
            )
            attachment_evidence = _input_attachment_evidence(
                chunks, approval, policy_snapshot
            )
            state = ApprovalService._initial_state(
                approval,
                fields,
                attachment_evidence,
                trajectory["hard_limits"],
                [ToolName(item) for item in trajectory["allowed_tools"]],
                policy_catalog_snapshot_id=policy_snapshot,
                structured_data_snapshot_id=structured_snapshot,
            )
            context = (
                ApprovalService(database)._tool_context(
                    state,
                    catalog_snapshot_id=policy_snapshot,
                    structured_data_snapshot_id=structured_snapshot,
                    allowed_tools=[ToolName(item) for item in trajectory["allowed_tools"]],
                )
                if args.active_catalog else _runtime_context(
                    {
                        "allowed_tools": trajectory["allowed_tools"],
                        "fixture_context": {
                            "policy_catalog_snapshot_id": policy_snapshot,
                            "structured_data_snapshot_id": structured_snapshot,
                        },
                    },
                    state,
                    chunks,
                )
            )
            started = time.perf_counter()
            error = None
            try:
                if args.variant in {"multi_agent", "no_supervisor"}:
                    output = graph.invoke(
                        state,
                        context=ApprovalRuntime(
                            model,
                            registry,
                            context,
                            database,
                            persist_workflow=False,
                            supervisor_mode=(
                                "reviewer_direct" if args.variant == "no_supervisor" else "llm"
                            ),
                        ),
                        config={
                            "configurable": {
                                "thread_id": f"p14:{args.variant}:{args.split}:{sample['sample_id']}"
                            },
                            "recursion_limit": 20,
                        },
                    )
                    interrupted = bool(output.pop("__interrupt__", []))
                    final = validate_state(output)
                else:
                    final = _baseline_run(args.variant, state, model, registry, context, database)
                    interrupted = final["status"] == ApprovalStatus.HUMAN_PENDING
            except Exception as exc:  # noqa: BLE001 - invalid attempts remain in the denominator
                error = f"{type(exc).__name__}: {exc}"
                interrupted = False
                final = None
            latency_ms = round((time.perf_counter() - started) * 1000)
            matched = _matched_ids(final["evidence"], reference) if final else set()
            review = final.get("evidence_review") if final else None
            cited_ids = (
                {evidence_id for item in review.coverage for evidence_id in item.evidence_ids}
                | set(review.conflict_evidence_ids)
                if review
                else set()
            )
            reviewer_cited_evidence = (
                _matched_ids(
                    [item for item in final["evidence"] if item.evidence_id in cited_ids],
                    reference,
                )
                if final
                else set()
            )
            agent_evidence = (
                _matched_ids([*attachment_evidence, *final["retrieved_candidates"]], reference)
                if final
                else set()
            )
            expected_rules = {
                (item["rule_id"], item["rule_version"], item["outcome"])
                for item in sample["expected_rule_results"]
            }
            actual_rules = (
                {
                    (item.rule_id, item.rule_version, item.outcome.value)
                    for item in final["rule_results"]
                }
                if final
                else set()
            )
            rules_match = expected_rules <= actual_rules and not any(
                actual[0] == expected[0] and actual != expected
                for expected in expected_rules
                for actual in actual_rules
            )
            recommendation = final.get("recommendation") if final else None
            raw_recommendation = recommendation.value if recommendation else None
            predicted = (
                None
                if raw_recommendation == "HUMAN_REVIEW"
                and final["status"] not in {
                    ApprovalStatus.HUMAN_PENDING,
                    ApprovalStatus.INSUFFICIENT_EVIDENCE,
                }
                else raw_recommendation
            )
            evidence_match = set(sample["expected_evidence_ids"]) <= matched
            coverage = (
                sum(
                    set(question["required_evidence_ids"]) <= agent_evidence
                    for question in trajectory["required_sub_questions"]
                )
                if final
                else 0
            )
            capability_match = _capabilities_met(trajectory, final) if final else False
            allowed = set(trajectory["allowed_tools"])
            tool_names = (
                [
                    call.tool_name.value
                    for step in final["agent_trajectory"]
                    for call in step.tool_calls
                ]
                if final
                else []
            )
            stop_reason = final.get("agent_stop_reason") if final else None
            stop_match = _stop_accepted(sample, stop_reason)
            risk_flags = set(final["risk_flags"]) if final else set()
            business_risks = risk_flags - {
                "TOOL_DEGRADED",
                "DUPLICATE_TOOL_CALL_BLOCKED",
                "RULE_VALIDATOR_DEGRADED",
            }
            success_without_stop = bool(
                final
                and final["status"] != ApprovalStatus.SYSTEM_ERROR
                and predicted == sample["expected_recommendation"]
                and evidence_match
                and rules_match
                and set(sample["expected_risk_flags"]) == business_risks
                and coverage == len(trajectory["required_sub_questions"])
                and capability_match
                and all(name in allowed for name in tool_names)
            )
            success = success_without_stop and stop_match
            row = {
                "sample_id": sample["sample_id"],
                "case_id": sample["case_id"],
                "source_policy_catalog_snapshot_id": sample["policy_catalog_snapshot_id"],
                "evaluated_policy_catalog_snapshot_id": policy_snapshot,
                "expected_recommendation": sample["expected_recommendation"],
                "recommendation": predicted,
                "raw_recommendation": raw_recommendation,
                "status": final["status"].value if final else "SYSTEM_ERROR",
                "human_interrupted": interrupted,
                "stop_reason": stop_reason,
                "stop_match": stop_match,
                "final_reason_codes": (
                    [item.code for item in final["decision_reasons"]] if final else []
                ),
                "matched_evidence_ids": sorted(matched),
                "agent_evidence_ids": sorted(agent_evidence),
                "reviewer_cited_evidence_ids": sorted(reviewer_cited_evidence),
                "evidence_match": evidence_match,
                "rule_match": rules_match,
                "risk_flags": sorted(risk_flags),
                "business_risk_match": set(sample["expected_risk_flags"]) == business_risks,
                "covered_subquestions": coverage,
                "total_subquestions": len(trajectory["required_sub_questions"]),
                "capability_match": capability_match,
                "tool_names": tool_names,
                "agent_steps": final["agent_step_count"] if final else None,
                "retrieval_rounds": final["retrieval_round_count"] if final else None,
                "query_rewrites": final["query_rewrite_count"] if final else None,
                "tokens": final["tokens_used"] if final else None,
                "latency_ms": latency_ms,
                "task_success": success,
                "task_success_without_stop": success_without_stop,
                "error": error,
                "raw_prediction": (
                    {
                        "extracted_fields": [item.model_dump(mode="json") for item in fields],
                        "agent_trajectory": [
                            item.model_dump(mode="json") for item in final["agent_trajectory"]
                        ],
                        "supervisor_decisions": [
                            item.model_dump(mode="json") for item in final["supervisor_decisions"]
                        ],
                        "guardrail_decisions": [
                            item.model_dump(mode="json") for item in final["guardrail_decisions"]
                        ],
                        "tool_observations": [
                            item.model_dump(mode="json") for item in final["tool_observations"]
                        ],
                        "evidence_review": review.model_dump(mode="json") if review else None,
                        "rule_results": [
                            item.model_dump(mode="json") for item in final["rule_results"]
                        ],
                        "decision_reasons": [
                            item.model_dump(mode="json") for item in final["decision_reasons"]
                        ],
                    }
                    if final
                    else None
                ),
            }
            rows.append(row)
            print(
                json.dumps({key: row[key] for key in ("sample_id", "status", "task_success")}),
                flush=True,
            )
    finally:
        index.close()
        database.close()

    classes = ("PASS_RECOMMENDED", "REJECT_RECOMMENDED", "HUMAN_REVIEW")
    class_support = {
        label: sum(row["expected_recommendation"] == label for row in rows) for label in classes
    }
    class_f1 = {}
    for label in classes:
        true_positive = sum(
            row["expected_recommendation"] == label and row["recommendation"] == label
            for row in rows
        )
        false_positive = sum(
            row["expected_recommendation"] != label and row["recommendation"] == label
            for row in rows
        )
        false_negative = class_support[label] - true_positive
        denominator = 2 * true_positive + false_positive + false_negative
        class_f1[label] = 2 * true_positive / denominator if denominator else None
    macro_f1 = (
        _metric("approval.macro_f1", sum(class_f1.values()), len(classes), args.split)
        if all(class_support.values())
        else {
            **_metric("approval.macro_f1", 0, 0, args.split),
            "status": "INVALID_RUN",
            "support": len(rows),
        }
    )
    metrics = [
        _metric(
            "agent.task_success_rate",
            sum(row["task_success"] for row in rows),
            len(rows),
            args.split,
        ),
        _metric(
            "agent.task_success_without_stop_rate",
            sum(row["task_success_without_stop"] for row in rows),
            len(rows),
            args.split,
        ),
        _metric(
            "agent.stop_reason_match_rate",
            sum(row["stop_match"] for row in rows),
            len(rows),
            args.split,
        ),
        _metric(
            "agent.subquestion_coverage",
            sum(
                row["covered_subquestions"] / row["total_subquestions"]
                for row in rows
                if row["total_subquestions"]
            ),
            sum(bool(row["total_subquestions"]) for row in rows),
            args.split,
        ),
        _metric(
            "agent.mean_steps",
            sum(row["agent_steps"] or 0 for row in rows),
            len(rows),
            args.split,
            direction="conditional",
            unit="steps",
        ),
        _metric(
            "agent.tool_selection_case_accuracy",
            sum(
                row["capability_match"]
                and set(row["tool_names"]) <= set(trajectories[row["case_id"]]["allowed_tools"])
                for row in rows
            ),
            len(rows),
            args.split,
        ),
        _metric(
            "approval.accuracy",
            sum(row["recommendation"] == row["expected_recommendation"] for row in rows),
            len(rows),
            args.split,
        ),
        macro_f1,
        _metric(
            "approval.false_auto_pass_rate",
            sum(
                row["recommendation"] == "PASS_RECOMMENDED"
                and row["expected_recommendation"] != "PASS_RECOMMENDED"
                for row in rows
            ),
            sum(row["expected_recommendation"] != "PASS_RECOMMENDED" for row in rows),
            args.split,
            direction="lower",
        ),
        _metric(
            "approval.rule_result_exact_match_rate",
            sum(row["rule_match"] for row in rows),
            len(rows),
            args.split,
        ),
        _metric(
            "approval.human_intervention_rate",
            sum(row["human_interrupted"] for row in rows),
            len(rows),
            args.split,
            direction="conditional",
        ),
        _metric(
            "approval.system_error_rate",
            sum(row["status"] == "SYSTEM_ERROR" for row in rows),
            len(rows),
            args.split,
            direction="lower",
        ),
    ]
    report = {
        "schema": "p14-workflow-evaluation/v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "split": args.split,
        "model": model.model,
        "prompt_versions": {
            "retrieval": RETRIEVAL_PROMPT_VERSION,
            "reviewer": EVIDENCE_REVIEW_PROMPT_VERSION,
            "supervisor": SUPERVISOR_PROMPT_VERSION,
        },
        "retrieval_mode": os.getenv("POLICY_RETRIEVAL_MODE", "dense"),
        "variant": args.variant,
        "catalog_mode": "ACTIVE_COMPANY" if args.active_catalog else "DATASET_SOURCE",
        "evaluated_policy_catalog_snapshot_id": (
            active_catalog()["policy_catalog_snapshot_id"] if args.active_catalog else None
        ),
        "sample_count": len(rows),
        "upstream_extraction": "P04 extractor on frozen parsed attachment chunks; gold fields hidden",
        "stop_equivalents": {key: sorted(value) for key, value in _STOP_EQUIVALENTS.items()},
        "class_support": class_support,
        "class_f1": class_f1,
        "metrics": metrics,
        "samples": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if all(row["error"] is None for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
