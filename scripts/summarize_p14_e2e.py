"""Join a Playwright run with persisted approval audit events."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import PostgresStore

SIDE_EFFECT_EVENTS = (
    "APPROVAL_FINALIZED",
    "HUMAN_REVIEWED",
    "NOTIFICATION_REQUESTED",
    "CASE_WRITE_REQUESTED",
)


def _tests(suites: list[dict]) -> list[dict]:
    found = []
    for suite in suites:
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                results = test.get("results", [])
                request_id = next(
                    (
                        item["description"]
                        for item in test.get("annotations", [])
                        if item["type"] == "request_id"
                    ),
                    None,
                )
                found.append(
                    {
                        "title": spec["title"],
                        "status": results[-1]["status"] if results else "missing",
                        "browser_duration_ms": sum(item["duration"] for item in results),
                        "request_id": request_id,
                    }
                )
        found.extend(_tests(suite.get("suites", [])))
    return found


def _audit(store: PostgresStore, request_id: str) -> dict:
    with store.engine.connect() as connection:
        events = (
            connection.execute(
                text(
                    "SELECT event_type, created_at, payload FROM audit_events "
                    "WHERE request_id = :request_id ORDER BY event_id"
                ),
                {"request_id": request_id},
            )
            .mappings()
            .all()
        )
        status = connection.execute(
            text("SELECT status FROM approval_requests WHERE request_id = :request_id"),
            {"request_id": request_id},
        ).scalar_one_or_none()
    counts = Counter(item["event_type"] for item in events)
    created = next(
        (item["created_at"] for item in events if item["event_type"] == "APPROVAL_CREATED"),
        None,
    )
    first_machine_terminal = next(
        (
            item
            for item in events
            if item["event_type"] in {"HUMAN_REVIEW_REQUESTED", "APPROVAL_FINALIZED"}
        ),
        None,
    )
    machine_latency_ms = (
        round((first_machine_terminal["created_at"] - created).total_seconds() * 1000)
        if created and first_machine_terminal
        else None
    )
    duplicates = sum(max(counts[event] - 1, 0) for event in SIDE_EFFECT_EVENTS)
    return {
        "business_status": status,
        "machine_terminal_event": (
            first_machine_terminal["event_type"] if first_machine_terminal else None
        ),
        "golden_smoke_machine_latency_ms": machine_latency_ms,
        "event_counts": {event: counts[event] for event in SIDE_EFFECT_EVENTS},
        "human_interrupt_count": counts["HUMAN_REVIEW_REQUESTED"],
        "duplicate_side_effects": duplicates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--playwright", type=Path, default=ROOT / "frontend/test-results/results.json"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "evals/reports/p14_frontend_e2e.json")
    args = parser.parse_args()
    source = args.playwright.read_bytes()
    raw = json.loads(source)
    tests = _tests(raw["suites"])
    store = PostgresStore()
    try:
        for item in tests:
            if item["request_id"]:
                item.update(_audit(store, item["request_id"]))
    finally:
        store.close()
    passed = all(item["status"] == "passed" for item in tests)
    duplicates = sum(item.get("duplicate_side_effects", 0) for item in tests)
    audit_complete = all(
        item.get("machine_terminal_event") is not None for item in tests if item["request_id"]
    )
    git = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    report = {
        "schema": "p14-frontend-e2e/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "MEASURED" if passed and audit_complete and duplicates == 0 else "INVALID_RUN",
        "playwright_raw_sha256": hashlib.sha256(source).hexdigest(),
        "git_commit": git,
        "git_dirty": bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        ),
        "model": os.getenv("AGENT_MODEL", "qwen3.8-flash"),
        "environment": {
            "platform": platform.platform(),
            "browser_channel": os.getenv("PLAYWRIGHT_CHANNEL", "msedge"),
            "concurrency": 1,
            "cache": "existing model/vector caches",
            "warmup": "none",
        },
        "test_count": len(tests),
        "passed_count": sum(item["status"] == "passed" for item in tests),
        "duplicate_side_effects": duplicates,
        "tests": tests,
        "limitations": [
            "Three public golden cases are a functional gate, not a statistical quality or P95 sample.",
            "Latency ends at the first persisted machine result or human interrupt; human waiting is excluded.",
            "Browser durations include navigation, polling, and human form interaction and are not machine P95.",
            "Model/network latency and cache state may vary between runs.",
        ],
        "performance_metrics_status": "NOT_MEASURED_ON_NON_GOLDEN_VALIDATION",
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# P14 browser E2E and persistence smoke",
        "",
        (
            f"- Result: {report['passed_count']}/{report['test_count']} Playwright tests; "
            f"duplicate side effects: {duplicates}."
        ),
        "- These golden-case durations are diagnostic only; no overall P95 is claimed.",
        "",
        "| Test | Browser result | Business status | Machine terminal | Machine ms |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for item in tests:
        lines.append(
            f"| {item['title']} | {item['status']} | {item.get('business_status', '—')} | "
            f"{item.get('machine_terminal_event', '—')} | "
            f"{item.get('golden_smoke_machine_latency_ms', '—')} |"
        )
    output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "passed": report["passed_count"]}))
    return 0 if report["status"] == "MEASURED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
