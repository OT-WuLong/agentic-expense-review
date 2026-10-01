"""Compare live validation decisions with and without one dev-seeded case memory."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.contracts import ToolName
from app.agents.model import StructuredChatClient
from app.approval_service import ApprovalService
from app.database import PostgresStore
from app.graph.approval import ApprovalRuntime, build_approval_graph
from app.graph.state import ApprovalState, validate_state
from app.ingestion.models import DocumentChunk
from app.memory import write_memory
from app.models import (
    ApprovalInput,
    ApprovalStatus,
    ExtractedField,
    FinalDecision,
    HumanReviewAction,
    Recommendation,
    RuleResult,
)
from app.retrieval import HybridPolicyIndex, QwenEmbeddingClient, QwenRerankerClient
from app.retrieval.dense import EmbeddingUsage
from app.tools import build_tool_registry
from scripts.run_golden_trajectories import _input_attachment_evidence, _runtime_context


class _SeedEmbedder:
    def embed(self, texts: list[str], *, text_type: str, timeout_seconds: float):
        return [[1.0] + [0.0] * 1023 for _ in texts], EmbeddingUsage()


def _samples(path: Path) -> list[dict]:
    return [
        item
        for line in path.read_text(encoding="utf-8").splitlines()
        if (item := json.loads(line))["split"] in {"dev", "validation"}
    ]


def _approval(sample: dict, request_id: str) -> ApprovalInput:
    source = sample["input"]
    application = {
        key: value for key, value in source["application"].items() if key != "request_id"
    }
    return ApprovalInput.model_validate(
        {
            "request_id": request_id,
            "applicant": source["applicant"],
            "application": application,
            "documents": source["documents"],
        }
    )


def _seed_dev_case(store: PostgresStore, sample: dict, request_id: str) -> None:
    """Seed an explicit synthetic human outcome; never touch frozen test records."""

    approval = _approval(sample, request_id)
    store.create_approval(
        request_id=request_id,
        thread_id=f"approval:{request_id}",
        idempotency_key=f"memory-ablation:{request_id}",
        applicant=approval.applicant,
        application=approval.application,
        documents=[],
    )
    decision = FinalDecision(
        action=HumanReviewAction.REJECT,
        operator_id="SYNTHETIC-REVIEWER",
        recommendation=Recommendation.REJECT_RECOMMENDED,
        previous_recommendation=Recommendation.HUMAN_REVIEW,
        reason="合成历史结案：缺少交通票据且未补交，人工驳回。",
    )
    store.finalize_approval(
        request_id=request_id,
        status=ApprovalStatus.COMPLETED,
        recommendation=Recommendation.REJECT_RECOMMENDED,
        risk_level=None,
        final_decision=decision,
        human_idempotency_key=request_id,
    )
    state = cast(
        ApprovalState,
        {
            "request_id": request_id,
            "applicant": approval.applicant,
            "application": approval.application,
            "recommendation": Recommendation.HUMAN_REVIEW,
            "final_decision": decision,
            "rule_results": [
                RuleResult.model_validate(item) for item in sample["expected_rule_results"]
            ],
            "evidence": [],
        },
    )
    write_memory(state, store, embedder=_SeedEmbedder())


def main() -> None:
    approval_samples = _samples(ROOT / "evals/datasets/approvals.jsonl")
    trajectories = {
        item["case_id"]: item
        for item in _samples(ROOT / "evals/datasets/agent_trajectories.jsonl")
        if item["split"] == "validation"
    }
    dev = next(item for item in approval_samples if item["split"] == "dev")
    validation = [item for item in approval_samples if item["split"] == "validation"]
    chunks = [
        DocumentChunk.model_validate_json(line)
        for line in (ROOT / "data/fixtures/p04_chunks.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    store = PostgresStore()
    index = HybridPolicyIndex(
        uri=os.getenv("MILVUS_URI", "http://localhost:19530"),
        chunks_path=ROOT / "data/fixtures/p04_chunks.jsonl",
        embedder=QwenEmbeddingClient(),
        reranker=QwenRerankerClient(),
        mode="dense",
    )
    registry = build_tool_registry(index, store)
    model = StructuredChatClient()
    graph = build_approval_graph(InMemorySaver())
    seed_request_id = f"REQ-P12-ABLATION-{uuid4().hex[:12]}"
    runs = []
    try:
        _seed_dev_case(store, dev, seed_request_id)
        for sample in validation:
            trajectory = trajectories[sample["case_id"]]
            for mode in ("without_memory", "with_memory"):
                request_id = sample["input"]["application"]["request_id"]
                approval = _approval(sample, request_id)
                fields = [
                    ExtractedField.model_validate(item)
                    for item in sample["expected_extracted_fields"]
                ]
                allowed = [ToolName.POLICY_SEARCH]
                if mode == "with_memory":
                    allowed.append(ToolName.CASE_SEARCH)
                state = ApprovalService._initial_state(
                    approval,
                    fields,
                    _input_attachment_evidence(
                        chunks, approval, sample["policy_catalog_snapshot_id"]
                    ),
                    trajectory["hard_limits"],
                    allowed,
                )
                context = _runtime_context(
                    {
                        "allowed_tools": [item.value for item in allowed],
                        "fixture_context": {
                            "policy_catalog_snapshot_id": sample["policy_catalog_snapshot_id"],
                            "structured_data_snapshot_id": sample.get(
                                "structured_data_snapshot_id", "STRUCTURED-DATA-REF-1.2.0"
                            ),
                        },
                    },
                    state,
                    chunks,
                )
                started = time.perf_counter()
                output = graph.invoke(
                    state,
                    context=ApprovalRuntime(
                        model, registry, context, store, persist_workflow=False
                    ),
                    config={
                        "configurable": {"thread_id": f"p12-ablation:{sample['sample_id']}:{mode}"},
                        "recursion_limit": 20,
                    },
                )
                interrupted = bool(output.pop("__interrupt__", []))
                result = validate_state(output)
                recommendation = result.get("recommendation")
                case_observations = [
                    item
                    for item in result["tool_observations"]
                    if item.tool_name == ToolName.CASE_SEARCH
                ]
                row = {
                    "sample_id": sample["sample_id"],
                    "mode": mode,
                    "expected_recommendation": sample["expected_recommendation"],
                    "recommendation": recommendation.value if recommendation else None,
                    "status": result["status"].value,
                    "task_success": recommendation is not None
                    and recommendation.value == sample["expected_recommendation"]
                    and result["status"] != ApprovalStatus.SYSTEM_ERROR,
                    "human_intervention": recommendation == Recommendation.HUMAN_REVIEW,
                    "human_interrupted": interrupted,
                    "case_search_calls": len(case_observations),
                    "case_search_hits": sum(len(item.items) for item in case_observations),
                    "stop_reason": result.get("agent_stop_reason"),
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                }
                runs.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        with store.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM audit_events WHERE request_id = :request_id"),
                {"request_id": seed_request_id},
            )
            connection.execute(
                text("DELETE FROM approval_requests WHERE request_id = :request_id"),
                {"request_id": seed_request_id},
            )
        index.close()
        store.close()

    summary = {}
    for mode in ("without_memory", "with_memory"):
        group = [item for item in runs if item["mode"] == mode]
        summary[mode] = {
            "task_success": f"{sum(item['task_success'] for item in group)}/{len(group)}",
            "human_intervention": f"{sum(item['human_intervention'] for item in group)}/{len(group)}",
            "system_errors": sum(item["status"] == "SYSTEM_ERROR" for item in group),
            "case_search_calls": sum(item["case_search_calls"] for item in group),
            "case_search_hits": sum(item["case_search_hits"] for item in group),
        }
    dev_application = dev["input"]["application"]
    seed_eligible_samples = [
        sample["sample_id"]
        for sample in validation
        if sample["input"]["applicant"]["department_id"]
        == dev["input"]["applicant"]["department_id"]
        and sample["input"]["application"]["expense_type"] == dev_application["expense_type"]
        and date.fromisoformat(dev_application["occurred_on"])
        <= date.fromisoformat(sample["input"]["application"]["occurred_on"])
    ]
    case_hits = sum(item["case_search_hits"] for item in runs)
    report = {
        "schema": "p12-case-memory-ablation/v2",
        "status": (
            "INCONCLUSIVE_SYSTEM_ERRORS"
            if any(item["status"] == "SYSTEM_ERROR" for item in runs)
            else "COMPLETE"
        ),
        "run_at": datetime.now(UTC).isoformat(),
        "model": model.model,
        "split": "validation only; frozen test untouched",
        "memory_seed": "one synthetic dev-split case with a simulated human rejection; removed after run",
        "seed_eligible_validation_samples": seed_eligible_samples,
        "memory_effect_status": "NOT_MEASURED_NO_CASE_HITS" if not case_hits else "DESCRIPTIVE",
        "limitations": (
            "Three samples, one dev-seeded case, one live run per mode. Tool enablement "
            "is not a measure of case-memory benefit unless a relevant case is returned."
        ),
        "summary": summary,
        "observed_effect": {
            "task_success_delta_cases": sum(
                item["task_success"] for item in runs if item["mode"] == "with_memory"
            )
            - sum(item["task_success"] for item in runs if item["mode"] == "without_memory"),
            "human_intervention_delta_cases": sum(
                item["human_intervention"] for item in runs if item["mode"] == "with_memory"
            )
            - sum(item["human_intervention"] for item in runs if item["mode"] == "without_memory"),
            "case_results_consumed": case_hits,
        },
        "runs": runs,
    }
    output_path = ROOT / "evals/reports/p12_case_memory_ablation.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
