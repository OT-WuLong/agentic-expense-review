"""Compare selected retrieval/control variants on the same dev or validation cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from math import ceil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = (
    "fixed_rag",
    "fixed_rewrite",
    "single_retrieval_agent",
    "no_supervisor",
    "multi_agent",
)


def _p95(values: list[int]) -> int | None:
    return sorted(values)[ceil(0.95 * len(values)) - 1] if values else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "validation"), default="validation")
    parser.add_argument("--dataset-dir", type=Path, default=ROOT / "evals/datasets")
    parser.add_argument("--attachment-chunks", type=Path)
    parser.add_argument("--attachment-evidence", type=Path)
    parser.add_argument("--active-catalog", action="store_true")
    parser.add_argument(
        "--variants", nargs="+", choices=(*VARIANTS, "single_agent_dense"), default=list(VARIANTS)
    )
    parser.add_argument("--output", type=Path, default=ROOT / "evals/reports/agentic_ablation.json")
    args = parser.parse_args()
    variants = tuple(args.variants)
    if len(set(variants)) != len(variants):
        parser.error("variants must be distinct")
    output = args.output.resolve()
    raw_dir = output.parent / f"{output.stem}_raw"
    if output.exists() or raw_dir.exists():
        parser.error("choose a new output path; prior runs are never overwritten")
    raw_dir.mkdir(parents=True)
    input_paths = {
        "approvals": args.dataset_dir / "approvals.jsonl",
        "trajectories": args.dataset_dir / "agent_trajectories.jsonl",
        "policy_chunks": ROOT / "data/fixtures/p04_chunks.jsonl",
        "active_catalog": ROOT / "data/fixtures/active_catalog.json",
    }
    if args.attachment_chunks:
        input_paths["attachment_chunks"] = args.attachment_chunks
    if args.attachment_evidence:
        input_paths["attachment_evidence"] = args.attachment_evidence
    input_hashes = {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in input_paths.items()
    }
    expected_sample_ids = {
        row["sample_id"]
        for line in input_paths["approvals"].read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line))["split"] == args.split
    }
    run_plan = {
        "model": os.getenv("AGENT_MODEL", "qwen3.8-flash"),
        "retrieval_mode": os.getenv("POLICY_RETRIEVAL_MODE", "dense"),
        "variants": variants,
        "dataset_dir": str(args.dataset_dir),
        "input_sha256": input_hashes,
        "primary_metrics": [
            "approval.accuracy",
            "comparison.business_success_rate",
            "approval.false_auto_pass_rate",
        ],
        "business_success_definition": (
            "Correct recommendation, required final evidence, rule outcomes, risk flags and allowed "
            "tools; excludes architecture-specific tool capability/call count and Agent stop labels."
        ),
        "single_agent_dense_definition": (
            "One agent with one unified prompt plans <=2 authorised tool calls and assesses the "
            "retrieved evidence in two LLM phases; one retrieval round, Dense policy search, "
            "no separate Supervisor or feedback loop. Shared extraction, scope, provenance checks, "
            "structured lookup availability and deterministic rules match multi_agent."
        ),
    }
    os.environ["PYTHONIOENCODING"] = "utf-8"
    (raw_dir / "run_plan.json").write_text(
        json.dumps(run_plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    executions = []
    reports = {}
    for variant in variants:
        report_path = raw_dir / f"{variant}.json"
        command = [
            sys.executable,
            str(ROOT / "scripts/evaluate_workflow.py"),
            "--split",
            args.split,
            "--dataset-dir",
            str(args.dataset_dir),
            "--variant",
            variant,
            "--output",
            str(report_path),
        ]
        if args.active_catalog:
            command.append("--active-catalog")
        for option, path in (
            ("--attachment-chunks", args.attachment_chunks),
            ("--attachment-evidence", args.attachment_evidence),
        ):
            if path:
                command.extend([option, str(path)])
        with (raw_dir / f"{variant}.stdout.txt").open("w", encoding="utf-8") as stdout_file:
            result = subprocess.run(
                command,
                cwd=ROOT,
                stdout=stdout_file,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
        (raw_dir / f"{variant}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        executions.append({"variant": variant, "exit_code": result.returncode})
        if report_path.exists():
            reports[variant] = json.loads(report_path.read_text(encoding="utf-8"))
        print(
            json.dumps(
                {"variant": variant, "exit_code": result.returncode, "report": report_path.exists()}
            ),
            flush=True,
        )
    sample_sets = {
        variant: {row["sample_id"] for row in report["samples"]}
        for variant, report in reports.items()
    }
    comparable = (
        len(reports) == len(variants)
        and bool(expected_sample_ids)
        and all(ids == expected_sample_ids for ids in sample_sets.values())
    )
    summaries = []
    for variant in variants:
        if variant not in reports:
            continue
        report = reports[variant]
        rows = report["samples"]
        metrics = {item["metric_id"]: item for item in report["metrics"]}
        summaries.append(
            {
                "variant": variant,
                "sample_count": len(rows),
                "task_success": f"{sum(row['task_success'] for row in rows)}/{len(rows)}",
                "business_success": f"{sum(row['business_success'] for row in rows)}/{len(rows)}",
                "business_success_rate": metrics["comparison.business_success_rate"]["value"],
                "approval_accuracy": metrics["approval.accuracy"]["value"],
                "subquestion_coverage": metrics["agent.subquestion_coverage"]["value"],
                "tool_selection_accuracy": metrics["agent.tool_selection_case_accuracy"]["value"],
                "mean_steps": metrics["agent.mean_steps"]["value"],
                "false_auto_pass_rate": metrics["approval.false_auto_pass_rate"]["value"],
                "human_intervention_rate": metrics["approval.human_intervention_rate"]["value"],
                "system_error_rate": metrics["approval.system_error_rate"]["value"],
                "attempt_latency_p95_ms": _p95([row["latency_ms"] for row in rows]),
                "nonerror_latency_p50_ms": (
                    sorted(row["latency_ms"] for row in rows if row["status"] != "SYSTEM_ERROR")[
                        ceil(0.5 * sum(row["status"] != "SYSTEM_ERROR" for row in rows)) - 1
                    ]
                    if any(row["status"] != "SYSTEM_ERROR" for row in rows)
                    else None
                ),
                "nonerror_latency_p95_ms": _p95(
                    [row["latency_ms"] for row in rows if row["status"] != "SYSTEM_ERROR"]
                ),
                "mean_agent_llm_tokens": sum(row["tokens"] or 0 for row in rows) / len(rows),
                "agent_llm_tokens": sum(row["tokens"] or 0 for row in rows),
                "tool_calls": sum(len(row["tool_names"]) for row in rows),
                "tool_errors": sum(
                    bool(item["error"])
                    for row in rows
                    if row["raw_prediction"]
                    for item in row["raw_prediction"]["tool_observations"]
                ),
                "guardrail_vetoes": sum(
                    len(row["raw_prediction"]["guardrail_decisions"])
                    for row in rows
                    if row["raw_prediction"]
                ),
            }
        )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    report = {
        "schema": "p14-agentic-ablation/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": (
            "MEASURED"
            if comparable and all(item["exit_code"] in {0, 1} for item in executions)
            else "INVALID_RUN"
        ),
        "split": args.split,
        "git_commit": commit,
        "git_dirty": bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        ),
        "dataset_sha256": input_hashes["approvals"],
        "trajectory_sha256": input_hashes["trajectories"],
        "run_plan": run_plan,
        "model": os.getenv("AGENT_MODEL", "qwen3.8-flash"),
        "prompt_versions": reports.get("multi_agent", {}).get("prompt_versions"),
        "retrieval_mode": os.getenv("POLICY_RETRIEVAL_MODE", "dense"),
        "environment": {"platform": platform.platform(), "cpu_count": os.cpu_count()},
        "same_sample_ids": comparable,
        "one_run_per_variant": True,
        "cost_status": "NOT_MEASURED_PROVIDER_PRICING_UNVERIFIED",
        "limitations": [
            "Author-created synthetic validation, one live run per variant; not external blind test.",
            "Recorded system errors stay in quality denominators; exit code 1 with a complete report is a measured failed workload.",
            "single_agent_dense vs multi_agent compares the whole prompt/control/retrieval-loop bundle, not agent count alone.",
            "Contractual strict success includes tool capabilities and stop labels; business_success is the architecture-neutral primary task metric.",
            "Fixed variants omit LLM review; only the no_supervisor pair isolates Supervisor removal.",
            "Mean steps count planning/review actions, including fixed planning.",
            "Agent token totals exclude failed model calls and embeddings; they are lower bounds, not cost metrics.",
            "Tool capability scoring uses distinct successful tool calls, not full semantic-purpose matching.",
            "Exact duplicate-query and monetary-cost metrics remain unmeasured.",
            "Attempt P95 includes system errors and is not contractual successful-task e2e P95.",
        ],
        "executions": executions,
        "variants": summaries,
        "raw_report_directory": str(raw_dir),
    }
    if {"single_agent_dense", "multi_agent"} <= set(reports) and comparable:
        baseline_rows = {row["sample_id"]: row for row in reports["single_agent_dense"]["samples"]}
        full_rows = {row["sample_id"]: row for row in reports["multi_agent"]["samples"]}
        paired = []
        for sample_id, base in baseline_rows.items():
            full = full_rows[sample_id]
            paired.append(
                {
                    "sample_id": sample_id,
                    "expense_type": full["expense_type"],
                    "expected": full["expected_recommendation"],
                    "baseline_recommendation": base["recommendation"],
                    "multi_agent_recommendation": full["recommendation"],
                    "baseline_business_success": base["business_success"],
                    "multi_agent_business_success": full["business_success"],
                    "baseline_missing_evidence": not base["evidence_match"],
                    "multi_agent_missing_evidence": not full["evidence_match"],
                }
            )
        base_summary, full_summary = (
            next(item for item in summaries if item["variant"] == variant)
            for variant in ("single_agent_dense", "multi_agent")
        )
        report["paired_comparison"] = {
            "sample_count": len(paired),
            "business_wins": sum(
                not r["baseline_business_success"] and r["multi_agent_business_success"]
                for r in paired
            ),
            "business_losses": sum(
                r["baseline_business_success"] and not r["multi_agent_business_success"]
                for r in paired
            ),
            "recommendation_delta_percentage_points": 100
            * (full_summary["approval_accuracy"] - base_summary["approval_accuracy"]),
            "business_success_delta_percentage_points": 100
            * (full_summary["business_success_rate"] - base_summary["business_success_rate"]),
            "samples": paired,
        }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Agentic ablation (validation)",
        "",
        f"- Status: `{report['status']}`; commit: `{commit}` (dirty={report['git_dirty']})",
        "- Same synthetic samples and hard limits; one live run per variant.",
        "- single_agent_dense uses one unified Agent prompt for planning and assessment, one retrieval round, and the same final rules. This compares the full workflow bundle, not agent count alone.",
        "- Cost and exact duplicate-query rate are not measured.",
        "",
        "| Variant | Business success | Strict success | Recommendation match | False auto-pass | System errors | Non-error P50 ms | Agent tokens* |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summaries:
        lines.append(
            f"| {item['variant']} | {item['business_success']} | {item['task_success']} | "
            f"{item['approval_accuracy']:.1%} | {item['false_auto_pass_rate']:.1%} | "
            f"{item['system_error_rate']:.1%} | {item['nonerror_latency_p50_ms']} | "
            f"{item['agent_llm_tokens']} |"
        )
    if report.get("paired_comparison"):
        paired = report["paired_comparison"]
        lines.extend(
            [
                "",
                f"- Recommendation difference: {paired['recommendation_delta_percentage_points']:+.2f} percentage points.",
                f"- Business success difference: {paired['business_success_delta_percentage_points']:+.2f} percentage points; paired wins/losses: {paired['business_wins']}/{paired['business_losses']}.",
                "- Business success requires correct suggestion, required final evidence, rule outcomes, risk flags and allowed tools; it excludes architecture-specific tool-call counts and stop labels.",
                "- Synthetic pre-parsed attachment inputs; one run per variant, not a raw-PDF end-to-end or external generalisation score.",
            ]
        )
    lines.extend(["", "*Agent tokens are a lower bound; failed calls and embeddings are excluded."])
    output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if report["status"] == "MEASURED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
