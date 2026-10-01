"""Run the P14 evaluation suites without opening the frozen test by accident."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import operator
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SUITES = ("retrieval", "agent", "approval", "security", "all")
OPERATORS = {">=": operator.ge, "<=": operator.le, "==": operator.eq}


def _passes_threshold(actual: float, comparison: str, expected: float) -> bool:
    return OPERATORS[comparison](actual, expected)


def _git_state() -> dict[str, object]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    return {"commit": commit, "dirty": dirty}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(args: argparse.Namespace, git: dict[str, object]) -> dict:
    from app.agents.evidence_reviewer import EVIDENCE_REVIEW_PROMPT_VERSION
    from app.agents.retrieval import RETRIEVAL_PROMPT_VERSION
    from app.agents.supervisor import SUPERVISOR_PROMPT_VERSION

    files = [
        "evals/datasets/retrieval.jsonl",
        "evals/datasets/agent_trajectories.jsonl",
        "evals/datasets/approvals.jsonl",
        "evals/datasets/security.jsonl",
        "evals/datasets/test.freeze.json",
        "evals/datasets/reference_evidence.json",
        "data/fixtures/p04_chunks.jsonl",
        "data/fixtures/golden_cases.json",
    ]
    if (ROOT / "evals/datasets/thresholds.freeze.json").exists():
        files.append("evals/datasets/thresholds.freeze.json")
    return {
        "schema": "p14-run-manifest/v1",
        "run_id": f"P14-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}",
        "started_at": datetime.now(UTC).isoformat(),
        "suite": args.suite,
        "split": args.split,
        "final_test": args.final_test,
        "git": git,
        "datasets": {item: _sha256(ROOT / item) for item in files},
        "dataset_version": json.loads(
            (ROOT / "evals/datasets/test.freeze.json").read_text(encoding="utf-8")
        )["dataset_version"],
        "models": {
            "agent": os.getenv("AGENT_MODEL", "qwen3.8-flash"),
            "embedding": "qwen3.7-text-embedding-flash",
            "reranker": "qwen3.7-text-rerank",
        },
        "prompts": {
            "retrieval": RETRIEVAL_PROMPT_VERSION,
            "supervisor": SUPERVISOR_PROMPT_VERSION,
            "reviewer": EVIDENCE_REVIEW_PROMPT_VERSION,
        },
        "parameters": {
            "policy_retrieval_mode": os.getenv("POLICY_RETRIEVAL_MODE", "dense"),
            "top_k": 5,
            "temperature": 0,
            "agent_budget_source": "agent_trajectories.jsonl hard_limits",
            "concurrency": 1,
            "cache": "existing local vector cache; no run-local clearing",
            "warmup": "none",
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
        },
    }


def _commands(args: argparse.Namespace, output_dir: Path) -> list[tuple[str, list[str], Path]]:
    suites = (
        ("retrieval", "workflow", "security")
        if args.suite == "all"
        else ("workflow" if args.suite in {"agent", "approval"} else args.suite,)
    )
    commands = []
    for suite in suites:
        output = output_dir / f"{suite}.json"
        script = {
            "retrieval": "evaluate_dense.py",
            "workflow": "evaluate_workflow.py",
            "security": "evaluate_p13_security.py",
        }[suite]
        commands.append(
            (
                suite,
                [
                    sys.executable,
                    str(ROOT / "scripts" / script),
                    "--split",
                    args.split,
                    "--output",
                    str(output),
                ],
                output,
            )
        )
    return commands


def _write_summary(output_dir: Path, manifest: dict, runs: list[dict]) -> None:
    metrics = []
    for run in runs:
        if not run["report_path"]:
            continue
        report = json.loads(Path(run["report_path"]).read_text(encoding="utf-8"))
        for item in report.get("metrics", []):
            metrics.append({**item, "run_id": manifest["run_id"], "suite": run["suite"]})
    columns = (
        "run_id",
        "suite",
        "metric_id",
        "status",
        "value",
        "numerator",
        "denominator",
        "support",
        "split",
        "slice",
    )
    with (output_dir / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(metrics)
    lines = [
        "# P14 evaluation summary",
        "",
        f"- Run: `{manifest['run_id']}`",
        f"- Commit: `{manifest['git']['commit']}` (dirty={manifest['git']['dirty']})",
        f"- Split: `{manifest['split']}`; model: `{manifest['models']['agent']}`",
        "- Raw predictions and full configuration are in the adjacent JSON files.",
        "- The tiny synthetic sample is descriptive, not a population estimate.",
        "",
        "| Suite | Metric | Value | Support | Status |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for item in metrics:
        value = "—" if item["value"] is None else f"{item['value']:.3f}"
        lines.append(
            f"| {item['suite']} | `{item['metric_id']}` | {value} | "
            f"{item['support']} | {item['status']} |"
        )
    lines.extend(["", "## Suite execution", ""])
    for run in runs:
        lines.append(
            f"- {run['suite']}: exit={run['exit_code']}; report={run['report_path'] or 'none'}"
        )
    if "threshold_results" in manifest:
        lines.extend(["", "## Frozen threshold checks", ""])
        for item in manifest["threshold_results"]:
            lines.append(
                f"- `{item['metric_id']}`: {item['actual']} {item['operator']} "
                f"{item['value']} → {'PASS' if item['passed'] else 'FAIL'}"
            )
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _threshold_results(runs: list[dict], frozen: dict) -> list[dict]:
    measured = {}
    for run in runs:
        if run["report_path"]:
            report = json.loads(Path(run["report_path"]).read_text(encoding="utf-8"))
            measured.update({item["metric_id"]: item for item in report.get("metrics", [])})
    results = []
    for threshold in frozen["thresholds"]:
        item = measured.get(threshold["metric_id"])
        actual = item.get("value") if item and item.get("status") == "MEASURED" else None
        results.append(
            {
                **threshold,
                "actual": actual,
                "passed": actual is not None
                and _passes_threshold(actual, threshold["operator"], threshold["value"]),
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=SUITES)
    parser.add_argument("--split", choices=("dev", "validation", "test"), default="validation")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "evals/reports/p14")
    parser.add_argument("--final-test", action="store_true")
    args = parser.parse_args()
    if args.split == "test" and not args.final_test:
        parser.error("frozen test requires --final-test")
    git = _git_state()
    frozen = None
    if args.final_test:
        if args.split != "test":
            parser.error("--final-test is only valid for the test split")
        if args.suite != "all":
            parser.error("final test must run all four evaluation families together")
        if git["dirty"]:
            parser.error("final test requires a clean, committed workspace")
        threshold_path = ROOT / "evals/datasets/thresholds.freeze.json"
        if not threshold_path.exists():
            parser.error("freeze validation thresholds before the final test")
        frozen = json.loads(threshold_path.read_text(encoding="utf-8"))
        if frozen.get("status") != "FROZEN_BEFORE_TEST":
            parser.error("freeze validation thresholds before the final test")
    manifest = _manifest(args, git)
    if frozen and frozen["configuration"] != {
        "dataset_version": manifest["dataset_version"],
        "agent_model": manifest["models"]["agent"],
        "retrieval_mode": manifest["parameters"]["policy_retrieval_mode"],
        "prompt_versions": manifest["prompts"],
    }:
        parser.error("final test configuration differs from the frozen validation plan")
    if frozen:
        if frozen["dataset_freeze_sha256"] != manifest["datasets"]["evals/datasets/test.freeze.json"]:
            parser.error("frozen test dataset manifest changed")
        encoded = json.dumps(
            frozen["configuration"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        if hashlib.sha256(encoded).hexdigest() != frozen["configuration_sha256"]:
            parser.error("frozen configuration hash changed")
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        parser.error("output directory must be empty to prevent mixing evaluation runs")
    output_dir.mkdir(parents=True, exist_ok=True)
    runs = []
    for suite, command, report_path in _commands(args, output_dir):
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        (output_dir / f"{suite}.stdout.txt").write_text(result.stdout, encoding="utf-8")
        (output_dir / f"{suite}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        runs.append(
            {
                "suite": suite,
                "exit_code": result.returncode,
                "report_path": str(report_path) if report_path.exists() else None,
            }
        )
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["runs"] = runs
    manifest["status"] = "MEASURED" if all(run["exit_code"] == 0 for run in runs) else "INVALID_RUN"
    if frozen:
        manifest["threshold_results"] = _threshold_results(runs, frozen)
        manifest["run_outcome"] = (
            "PASS"
            if manifest["status"] == "MEASURED"
            and all(item["passed"] for item in manifest["threshold_results"])
            else "FAIL"
        )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_summary(output_dir, manifest, runs)
    print(json.dumps({"status": manifest["status"], "output_dir": str(output_dir)}))
    return (
        0
        if manifest["status"] == "MEASURED" and manifest.get("run_outcome", "PASS") == "PASS"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
