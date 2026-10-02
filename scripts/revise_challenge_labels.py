"""Create a policy-grounded 1.6.1 label revision; never read predictions or rewrite 1.6.0."""

import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import build_challenge_dataset as builder

SOURCE = ROOT / "evals/datasets/challenge_1_6"
TARGET = ROOT / "evals/datasets/challenge_1_6_1"
VERSION = "1.6.1"


def main() -> None:
    if TARGET.exists():
        raise SystemExit("Revision already exists; use a new version instead of overwriting it.")
    reference = json.loads((SOURCE / "attachment_evidence.json").read_text(encoding="utf-8"))
    evidence = {item["evidence_id"]: item for item in reference["items"]}
    chunks = builder.rows(ROOT / "data/fixtures/p04_chunks.jsonl")
    city_equivalent = builder.policy_anchor(chunks, "OPS", "第十九条 ", evidence)
    approvals = deepcopy(builder.rows(SOURCE / "approvals.jsonl"))
    trajectories = deepcopy(builder.rows(SOURCE / "agent_trajectories.jsonl"))
    retrieval = deepcopy(builder.rows(SOURCE / "retrieval.jsonl"))
    by_case = {row["case_id"]: row for row in trajectories}
    retrieval_by_case = {row["case_id"]: row for row in retrieval}
    changes = []
    specs = {f"APP-C16-{item['slug']}": item for item in builder.CASES}
    for sample in approvals:
        original_stops = sample["acceptable_stop_reasons"][:]
        trajectory = by_case[sample["case_id"]]
        search = retrieval_by_case[sample["case_id"]]
        spec = specs[sample["sample_id"]]
        groups = []
        if spec["kind"] == "HOTEL" and spec["department"] in {builder.OPS, builder.MKT}:
            # Both articles require the finance classification effective on the stay date.
            # The dated city record is still a separate mandatory evidence item.
            for prefix in ("第二十一条", "第二十四条"):
                original = f"EVID-C16-HOTEL-{prefix}"
                if original not in sample["expected_evidence_ids"]:
                    continue
                group = {
                    "group_id": f"{sample['sample_id']}-{prefix}",
                    "any_of": [original, city_equivalent],
                }
                groups.append(group)
                sample["expected_evidence_ids"].remove(original)
                trajectory["required_evidence_ids"].remove(original)
                for question in trajectory["required_sub_questions"]:
                    if original in question["required_evidence_ids"]:
                        question["required_evidence_ids"].remove(original)
                        question.setdefault("acceptable_evidence_groups", []).append(group)
                for group_ in search["relevant_evidence_groups"]:
                    if original in group_["any_of"]:
                        group_["any_of"].append(city_equivalent)
            sample["acceptable_evidence_groups"] = groups
            trajectory["acceptable_evidence_groups"] = groups
        if spec["outcome"] == "HUMAN" and (
            spec.get("conference")
            or spec.get("duplicate")
            or "OVERDUE_EXCEPTION_REQUIRED" in sample["expected_risk_flags"]
        ):
            # Retrieval can finish successfully before the rule engine requires human discretion.
            sample["acceptable_stop_reasons"].append("ALL_NECESSARY_EVIDENCE_COVERED")
            trajectory["acceptable_stop_reasons"] = sample["acceptable_stop_reasons"][:]
        if groups or sample["acceptable_stop_reasons"] != original_stops:
            changes.append(
                {
                    "sample_id": sample["sample_id"],
                    "equivalent_groups": groups,
                    "acceptable_stop_reasons": sample["acceptable_stop_reasons"],
                }
            )
    TARGET.mkdir(parents=True)
    for name, values in [
        ("approvals", approvals),
        ("agent_trajectories", trajectories),
        ("retrieval", retrieval),
    ]:
        for row in values:
            row.update(dataset_version=VERSION, source_dataset_version="1.6.0")
        builder.write_jsonl(TARGET / f"{name}.jsonl", values)
    reference.update(dataset_version=VERSION, items=list(evidence.values()))
    builder.dump(TARGET / "attachment_evidence.json", reference)
    for name in ("attachment_manifest", "structured_snapshots", "case_index"):
        data = json.loads((SOURCE / f"{name}.json").read_text(encoding="utf-8"))
        data["dataset_version"] = VERSION
        if name == "case_index":
            by_sample = {row["sample_id"]: row for row in approvals}
            for case in data["cases"]:
                sample = by_sample[case["sample_id"]]
                case["policy_evidence_ids"] = [
                    eid for eid in sample["expected_evidence_ids"]
                    if evidence[eid]["source_type"] == "POLICY_DOCUMENT"
                ]
                case["acceptable_evidence_groups"] = sample.get("acceptable_evidence_groups", [])
        builder.dump(TARGET / f"{name}.json", data)
    builder.write_jsonl(
        TARGET / "attachment_chunks.jsonl", builder.rows(SOURCE / "attachment_chunks.jsonl")
    )
    builder.dump(
        TARGET / "label_changes.json",
        {
            "dataset_version": VERSION,
            "parent_version": "1.6.0",
            "basis": "已发布通用/部门条款与检索完成后规则转人工的既有流程语义；不读取预测结果",
            "recommendations_and_rule_outcomes_changed": False,
            "independently_reviewed": False,
            "changes": changes,
        },
    )
    summary = json.loads((SOURCE / "summary.json").read_text(encoding="utf-8"))
    summary.pop("latest_comparison_report", None)
    summary.update(dataset_version=VERSION, status="DRAFT_LABEL_REVISION_NOT_LIVE_EVALUATED")
    builder.dump(TARGET / "summary.json", summary)
    builder.TARGET, builder.COMBINED, builder.VERSION = (
        TARGET,
        ROOT / "evals/datasets/draft_1_6_1",
        VERSION,
    )
    builder.combine()
    combined_summary = json.loads((builder.COMBINED / "summary.json").read_text(encoding="utf-8"))
    combined_summary["source_datasets"] = ["draft_1_5_2", "challenge_1_6_1"]
    builder.dump(builder.COMBINED / "summary.json", combined_summary)
    print(
        f"Created {VERSION}: {len(approvals)} unchanged transactions; {len(changes)} label-contract revisions."
    )


if __name__ == "__main__":
    main()
