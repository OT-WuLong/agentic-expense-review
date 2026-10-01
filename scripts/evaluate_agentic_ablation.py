"""Compare five retrieval/control variants on the same dev or validation cases."""

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
    parser.add_argument("--output", type=Path, default=ROOT / "evals/reports/agentic_ablation.json")
    args = parser.parse_args()
    output = args.output.resolve()
    raw_dir = output.parent / f"{output.stem}_raw"
    if output.exists() or raw_dir.exists():
        parser.error("choose a new output path; prior runs are never overwritten")
    raw_dir.mkdir(parents=True)
    executions = []
    reports = {}
    for variant in VARIANTS:
        report_path = raw_dir / f"{variant}.json"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/evaluate_workflow.py"),
                "--split",
                args.split,
                "--variant",
                variant,
                "--output",
                str(report_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        (raw_dir / f"{variant}.stdout.txt").write_text(result.stdout, encoding="utf-8")
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
        len(reports) == len(VARIANTS) and len(set(map(frozenset, sample_sets.values()))) == 1
    )
    summaries = []
    for variant in VARIANTS:
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
                "approval_accuracy": metrics["approval.accuracy"]["value"],
                "subquestion_coverage": metrics["agent.subquestion_coverage"]["value"],
                "tool_selection_accuracy": metrics["agent.tool_selection_case_accuracy"]["value"],
                "mean_steps": metrics["agent.mean_steps"]["value"],
                "false_auto_pass_rate": metrics["approval.false_auto_pass_rate"]["value"],
                "human_intervention_rate": metrics["approval.human_intervention_rate"]["value"],
                "system_error_rate": metrics["approval.system_error_rate"]["value"],
                "attempt_latency_p95_ms": _p95([row["latency_ms"] for row in rows]),
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
            if comparable and all(item["exit_code"] == 0 for item in executions)
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
        "dataset_sha256": hashlib.sha256(
            (ROOT / "evals/datasets/approvals.jsonl").read_bytes()
        ).hexdigest(),
        "trajectory_sha256": hashlib.sha256(
            (ROOT / "evals/datasets/agent_trajectories.jsonl").read_bytes()
        ).hexdigest(),
        "model": os.getenv("AGENT_MODEL", "qwen3.8-flash"),
        "prompt_versions": reports.get("multi_agent", {}).get("prompt_versions"),
        "retrieval_mode": os.getenv("POLICY_RETRIEVAL_MODE", "dense"),
        "environment": {"platform": platform.platform(), "cpu_count": os.cpu_count()},
        "same_sample_ids": comparable,
        "one_run_per_variant": True,
        "cost_status": "NOT_MEASURED_PROVIDER_PRICING_UNVERIFIED",
        "limitations": [
            "Three synthetic validation cases are descriptive, not a population-level effect estimate.",
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
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Agentic ablation (validation)",
        "",
        f"- Status: `{report['status']}`; commit: `{commit}` (dirty={report['git_dirty']})",
        "- Same synthetic samples and hard limits; one live run per variant.",
        "- Only multi_agent vs no_supervisor isolates the LLM Supervisor; other baselines also remove review stages.",
        "- Cost and exact duplicate-query rate are not measured.",
        "",
        "| Variant | Task success | Approval accuracy | Coverage | Steps | Attempt P95 ms | System error rate | Agent tokens* |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summaries:
        lines.append(
            f"| {item['variant']} | {item['task_success']} | "
            f"{item['approval_accuracy']:.3f} | {item['subquestion_coverage']:.3f} | "
            f"{item['mean_steps']:.2f} | {item['attempt_latency_p95_ms']} | "
            f"{item['system_error_rate']:.3f} | {item['agent_llm_tokens']} |"
        )
    lines.extend(["", "*Agent tokens are a lower bound; failed calls and embeddings are excluded."])
    output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if report["status"] == "MEASURED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
