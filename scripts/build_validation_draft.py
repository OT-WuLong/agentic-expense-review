"""Build the unfrozen 1.4 validation matrix from existing traceable PDF fixtures."""

from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingestion.models import ParsedBlock
from app.ingestion.pipeline import canonicalize_and_chunk
from scripts.validate_datasets import read_bundle, validate_bundle

SOURCE = ROOT / "evals/datasets"
TARGET = SOURCE / "draft_1_4"
VERSION = "1.4.0"
INVOICES = (
    {
        "slug": "MEAL-01",
        "department": "DEPT-MARKETING",
        "date": "2026-05-26",
        "amount": "150.00",
        "attendees": 1,
        "description": "客户需求讨论后的工作餐",
    },
    {
        "slug": "MEAL-02",
        "department": "DEPT-MARKETING",
        "date": "2026-05-27",
        "amount": "300.00",
        "attendees": 2,
        "description": "合作方项目复盘工作餐",
    },
    {
        "slug": "MEAL-03",
        "department": "DEPT-OPERATIONS",
        "date": "2026-05-28",
        "amount": "240.00",
        "attendees": 2,
        "description": "现场服务沟通后的工作餐",
    },
    {
        "slug": "TAXI-01",
        "department": "DEPT-OPERATIONS",
        "date": "2026-05-26",
        "amount": "128.00",
        "route": "客户服务中心至合作园区",
        "description": "现场服务巡检",
    },
    {
        "slug": "TAXI-02",
        "department": "DEPT-OPERATIONS",
        "date": "2026-05-27",
        "amount": "288.00",
        "route": "研发园区至客户服务中心",
        "description": "客户现场设备检查",
    },
    {
        "slug": "TAXI-03",
        "department": "DEPT-MARKETING",
        "date": "2026-05-28",
        "amount": "295.00",
        "route": "会展中心至合作园区",
        "description": "客户活动现场协调",
    },
)


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def rule(rule_id: str, outcome: str, *evidence_ids: str) -> dict:
    return {
        "rule_id": rule_id,
        "rule_version": "1.0",
        "producer": "RULE_VALIDATOR",
        "outcome": outcome,
        "evidence_ids": list(evidence_ids),
    }


def independent_invoices() -> tuple[list[dict], list[dict], list[dict]]:
    """Bind six authored PDFs to source metadata, citable evidence, and parsed chunks."""

    attachments: list[dict] = []
    evidence: list[dict] = []
    chunks: list[dict] = []
    for item in INVOICES:
        slug = item["slug"]
        document_id = f"DOC-V14-{slug}"
        source = TARGET / "attachments" / f"{document_id}.md"
        pdf = source.with_suffix(".pdf")
        reader = PdfReader(pdf)
        if len(reader.pages) != 1:
            raise ValueError(f"{document_id}: expected one PDF page")
        page_text = reader.pages[0].extract_text()
        metadata = {
            "document_id": document_id,
            "fixture_key": f"SYNTHETIC_V14_{slug.replace('-', '_')}",
            "document_type": "MEAL_INVOICE" if slug.startswith("MEAL") else "TAXI_INVOICE",
            "version": "1",
            "page_count": 1,
            "authoring_path": source.relative_to(ROOT).as_posix(),
            "pdf_path": pdf.relative_to(ROOT).as_posix(),
            "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
            "media_type": "application/pdf",
            "source_type": "PROJECT_AUTHORED_SYNTHETIC",
            "license_id": "PROJECT-INTERNAL-SYNTHETIC",
            "synthetic": True,
        }
        attachments.append(metadata)
        excerpt = (
            f"实际参加人数：{item['attendees']} 人\n票面金额：{item['amount']} 元"
            if slug.startswith("MEAL")
            else f"路线：{item['route']}\n业务目的：{item['description']}\n员工实际支付：{item['amount']} 元"
        )
        evidence.append(
            {
                "evidence_id": f"EVID-V14-{slug}-INVOICE",
                "source_type": "ATTACHMENT",
                "document_id": document_id,
                "version": "1",
                "page": 1,
                "excerpt": excerpt,
            }
        )
        _, parsed = canonicalize_and_chunk(
            [ParsedBlock(page_idx=0, block_index=0, kind="text", text=page_text)],
            metadata,
            parse_artifact_sha256=hashlib.sha256(page_text.encode()).hexdigest(),
        )
        chunks.extend(chunk.model_dump(mode="json") for chunk in parsed)
    return attachments, evidence, chunks


def add_independent_cases(approvals: list[dict], trajectories: list[dict]) -> None:
    """Hand-label six straightforward passing applications against their own PDFs."""

    common = {
        key: approvals[0][key]
        for key in (
            "source_type",
            "synthesis_method",
            "license_id",
            "license_scope",
            "reference_source_ids",
            "policy_catalog_snapshot_id",
        )
    }
    for item in INVOICES:
        slug = item["slug"]
        meal = slug.startswith("MEAL")
        document_id = f"DOC-V14-{slug}"
        invoice_id = f"EVID-V14-{slug}-INVOICE"
        policy_id = "EVID-DEPT-MEAL-LIMIT" if meal else "EVID-DEPT-TRANSPORT-RULE"
        expense_type = "餐饮" if meal else "交通"
        application = {
            "request_id": f"REQ-V14-{slug}",
            "expense_type": expense_type,
            "currency": "CNY",
            "amount": item["amount"],
            "occurred_on": item["date"],
            "submitted_on": (date.fromisoformat(item["date"]) + timedelta(days=1)).isoformat(),
            "description": item["description"]
            if meal
            else f"{item['description']}：{item['route']}",
        }
        if meal:
            application["attendee_count"] = item["attendees"]
        fields = [
            {
                "field": field,
                "status": "PRESENT",
                "value": value,
                "raw_value": value,
                "document_id": document_id,
                "page": 1,
            }
            for field, value in (
                ("invoice_number", f"SYN-V14-{slug}"),
                ("amount", item["amount"]),
                ("occurred_on", item["date"]),
            )
        ]
        if not meal:
            fields.append(
                {
                    "field": "route",
                    "status": "PRESENT",
                    "value": f"路线：{item['route']}",
                    "raw_value": f"路线：{item['route']}",
                    "document_id": document_id,
                    "page": 1,
                }
            )
        expected_rules = (
            [
                rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "PASS", invoice_id),
                rule("RULE-OPS-MKT-MEAL-LIMIT", "PASS", policy_id),
            ]
            if meal
            else [
                rule("RULE-AMOUNT-DATE-CONSISTENCY", "PASS", invoice_id),
                rule("RULE-OPS-MKT-TRANSPORT-LIMIT", "PASS", policy_id, invoice_id),
            ]
        )
        identity = {
            **common,
            "dataset_version": VERSION,
            "split": "validation",
            "leakage_group_id": f"LG-VAL-V14-{slug}",
            "clause_family_id": "OPS-MKT-MEAL-LIMIT" if meal else "OPS-MKT-TRANSPORT-LIMIT",
            "case_id": f"CASE-VAL-V14-{slug}",
        }
        approvals.append(
            {
                **identity,
                "sample_id": f"APP-VAL-V14-{slug}",
                "input": {
                    "applicant": {
                        "employee_id": f"EMP-V14-{slug}",
                        "department_id": item["department"],
                        "display_name": "合成员工",
                    },
                    "application": application,
                    "documents": [
                        {
                            "document_id": document_id,
                            "fixture_key": f"SYNTHETIC_V14_{slug.replace('-', '_')}",
                            "document_type": "MEAL_INVOICE" if meal else "TAXI_INVOICE",
                            "media_type": "application/pdf",
                            "synthetic": True,
                        }
                    ],
                },
                "expected_extracted_fields": fields,
                "expected_evidence_ids": [policy_id, invoice_id],
                "expected_rule_results": expected_rules,
                "expected_risk_flags": [],
                "expected_recommendation": "PASS_RECOMMENDED",
                "acceptable_stop_reasons": ["EVIDENCE_SUFFICIENT"],
            }
        )
        trajectories.append(
            {
                **identity,
                "sample_id": f"TRAJ-VAL-V14-{slug}",
                "order_independent": True,
                "allowed_tools": ["policy_search"],
                "hard_limits": {
                    "max_agent_steps": 5,
                    "max_retrieval_rounds": 3,
                    "max_query_rewrites": 2,
                },
                "required_sub_questions": [
                    {
                        "intent": "APPLICABLE_POLICY_BOUNDARY",
                        "text": "核对本次费用的适用限额与票据材料。",
                        "required_evidence_ids": [policy_id],
                    }
                ],
                "required_tool_capabilities": [
                    {
                        "selected_tool_name": "policy_search",
                        "purpose": "FIND_APPLICABLE_POLICY",
                        "query_type": "",
                    }
                ],
                "required_evidence_ids": [policy_id, invoice_id],
                "acceptable_stop_reasons": ["EVIDENCE_SUFFICIENT"],
                "forbidden_actions": ["FABRICATE_EVIDENCE", "ISSUE_FINAL_APPROVAL"],
            }
        )


def main() -> None:
    approvals = [
        {**deepcopy(row), "dataset_version": VERSION}
        for row in rows(SOURCE / "approvals.jsonl")
        if row["split"] == "validation"
    ]
    trajectories = [
        {**deepcopy(row), "dataset_version": VERSION}
        for row in rows(SOURCE / "agent_trajectories.jsonl")
        if row["split"] == "validation"
    ]
    retrieval = [
        {**deepcopy(row), "dataset_version": VERSION}
        for row in rows(SOURCE / "retrieval.jsonl")
        if row["split"] == "validation"
    ]

    # The spent 1.2 test PDFs are now explicit validation fixtures, never a new holdout.
    legacy_approvals = {
        row["sample_id"]: row for row in rows(SOURCE / "archive/1.2.0/approvals.jsonl")
    }
    legacy_trajectories = {
        row["case_id"]: row for row in rows(SOURCE / "archive/1.2.0/agent_trajectories.jsonl")
    }
    legacy = (
        (
            "APP-TEST-MEAL-PASS",
            "MEAL-PASS",
            [
                rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "PASS", "EVID-MKT-MEAL-PASS-INVOICE"),
                rule("RULE-OPS-MKT-MEAL-LIMIT", "PASS", "EVID-DEPT-MEAL-LIMIT"),
            ],
            [],
        ),
        (
            "APP-TEST-MEAL-MISMATCH",
            "MEAL-MISMATCH",
            [
                rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "FAIL", "EVID-MKT-MEAL-MISMATCH-INVOICE"),
                rule("RULE-OPS-MKT-MEAL-LIMIT", "PASS", "EVID-DEPT-MEAL-LIMIT"),
            ],
            [],
        ),
        (
            "APP-TEST-LODGING-HUMAN",
            "HOTEL-EXCEPTION",
            [
                rule(
                    "RULE-LODGING-AMOUNT-LIMIT",
                    "FAIL",
                    "EVID-DEPT-LODGING-LIMIT",
                    "EVID-DEPT-HOTEL-INVOICE",
                )
            ],
            [],
        ),
    )
    for old_id, slug, expected_rules, flags in legacy:
        approval = deepcopy(legacy_approvals[old_id])
        old_case_id = approval["case_id"]
        approval.update(
            sample_id=f"APP-VAL-{slug}",
            case_id=f"CASE-VAL-{slug}",
            split="validation",
            dataset_version=VERSION,
            expected_rule_results=expected_rules,
            expected_risk_flags=flags,
        )
        approvals.append(approval)
        trajectory = deepcopy(legacy_trajectories[old_case_id])
        trajectory.update(
            sample_id=f"TRAJ-VAL-{slug}",
            case_id=approval["case_id"],
            split="validation",
            dataset_version=VERSION,
        )
        trajectories.append(trajectory)

    by_approval = {row["sample_id"]: row for row in approvals}
    by_trajectory = {row["case_id"]: row for row in trajectories}

    def variant(
        base_id: str,
        slug: str,
        changes: dict,
        recommendation: str,
        expected_rules: list[dict],
        *,
        evidence_ids: list[str] = (),
        flags: list[str] = (),
    ) -> None:
        base = by_approval[base_id]
        approval = deepcopy(base)
        approval.update(
            sample_id=f"APP-VAL-{slug}",
            case_id=f"CASE-VAL-{slug}",
            expected_rule_results=expected_rules,
            expected_risk_flags=list(flags),
            expected_recommendation=recommendation,
            acceptable_stop_reasons=(
                ["CLEAR_RULE_FAILURE", "EVIDENCE_SUFFICIENT"]
                if recommendation == "REJECT_RECOMMENDED"
                else ["MATERIALS_UNAVAILABLE", "EVIDENCE_ESCALATION", "MANDATORY_HUMAN_EXCEPTION"]
                if recommendation == "HUMAN_REVIEW"
                else ["EVIDENCE_SUFFICIENT"]
            ),
        )
        approval["input"]["application"].update(request_id=f"REQ-V14-{slug}", **changes)
        trajectory = deepcopy(by_trajectory[base["case_id"]])
        trajectory.update(
            sample_id=f"TRAJ-VAL-{slug}",
            case_id=approval["case_id"],
            acceptable_stop_reasons=approval["acceptable_stop_reasons"],
        )
        approval["expected_evidence_ids"] = list(
            dict.fromkeys(
                [
                    *trajectory["required_evidence_ids"],
                    *evidence_ids,
                    *(eid for item in expected_rules for eid in item["evidence_ids"]),
                ]
            )
        )
        approvals.append(approval)
        trajectories.append(trajectory)

    taxi = "APP-VAL-SALES-TAXI-PASS"
    commute = "APP-VAL-SALES-COMMUTE-REJECT"
    meal = "APP-VAL-MEAL-PASS"
    meal_mismatch = "APP-VAL-MEAL-MISMATCH"
    hotel = "APP-VAL-HOTEL-EXCEPTION"
    taxi_invoice = "EVID-SALES-TAXI-AMOUNT"
    commute_invoice = "EVID-SALES-COMMUTE-ROUTE"
    meal_invoice = "EVID-MKT-MEAL-PASS-INVOICE"
    mismatch_invoice = "EVID-MKT-MEAL-MISMATCH-INVOICE"
    hotel_invoice = "EVID-DEPT-HOTEL-INVOICE"

    variant(
        taxi,
        "TAXI-AMOUNT-MISMATCH",
        {"amount": "240.00"},
        "REJECT_RECOMMENDED",
        [rule("RULE-SALES-TICKET-CONSISTENCY", "FAIL", taxi_invoice)],
    )
    variant(
        taxi,
        "TAXI-OVER-LIMIT",
        {"amount": "275.00"},
        "REJECT_RECOMMENDED",
        [rule("RULE-SALES-TAXI-LIMIT", "FAIL", "EVID-SALES-TAXI-LIMIT", taxi_invoice)],
    )
    variant(
        taxi,
        "TAXI-DATE-MISMATCH",
        {"occurred_on": "2026-04-19", "submitted_on": "2026-04-20"},
        "REJECT_RECOMMENDED",
        [rule("RULE-SALES-TICKET-CONSISTENCY", "FAIL", taxi_invoice)],
    )
    variant(
        commute,
        "COMMUTE-FALSE-PURPOSE",
        {"description": "客户拜访交通费，申请人称本次并非通勤"},
        "REJECT_RECOMMENDED",
        [rule("RULE-SALES-PRIVATE-COMMUTE", "FAIL", "EVID-SALES-COMMUTE-BAN", commute_invoice)],
        flags=["CONFIRMED_PRIVATE_COMMUTE"],
    )
    variant(
        commute,
        "COMMUTE-AMOUNT-MISMATCH",
        {"amount": "45.00"},
        "REJECT_RECOMMENDED",
        [
            rule("RULE-SALES-TICKET-CONSISTENCY", "FAIL", "EVID-SALES-COMMUTE-AMOUNT"),
            rule("RULE-SALES-PRIVATE-COMMUTE", "FAIL", "EVID-SALES-COMMUTE-BAN", commute_invoice),
        ],
        flags=["CONFIRMED_PRIVATE_COMMUTE"],
    )
    variant(
        meal,
        "MEAL-NO-HEADCOUNT",
        {"attendee_count": None, "description": "客户项目工作餐，参与人数待补充"},
        "HUMAN_REVIEW",
        [rule("RULE-OPS-MKT-MEAL-LIMIT", "INDETERMINATE", "EVID-DEPT-MEAL-LIMIT")],
    )
    variant(
        meal,
        "MEAL-HEADCOUNT-CONFLICT",
        {"attendee_count": 2, "description": "客户工作餐，申请填写两人参与"},
        "HUMAN_REVIEW",
        [rule("RULE-OPS-MKT-MEAL-LIMIT", "PASS", "EVID-DEPT-MEAL-LIMIT")],
        evidence_ids=[meal_invoice],
    )
    variant(
        meal,
        "MEAL-AMOUNT-MISMATCH",
        {"amount": "160.00"},
        "REJECT_RECOMMENDED",
        [rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "FAIL", meal_invoice)],
    )
    variant(
        meal_mismatch,
        "MEAL-AMOUNT-CORRECTED",
        {"amount": "120.00"},
        "PASS_RECOMMENDED",
        [
            rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "PASS", mismatch_invoice),
            rule("RULE-OPS-MKT-MEAL-LIMIT", "PASS", "EVID-DEPT-MEAL-LIMIT"),
        ],
    )
    variant(
        meal_mismatch,
        "MEAL-SECOND-HEADCOUNT-CONFLICT",
        {"amount": "120.00", "attendee_count": 2, "description": "业务餐饮，申请填写两人参与"},
        "HUMAN_REVIEW",
        [rule("RULE-OPS-MKT-MEAL-LIMIT", "PASS", "EVID-DEPT-MEAL-LIMIT")],
        evidence_ids=[mismatch_invoice],
    )
    variant(
        meal_mismatch,
        "MEAL-DOUBLE-MISMATCH",
        {"amount": "180.00"},
        "REJECT_RECOMMENDED",
        [
            rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "FAIL", mismatch_invoice),
            rule("RULE-OPS-MKT-MEAL-LIMIT", "FAIL", "EVID-DEPT-MEAL-LIMIT"),
        ],
    )
    variant(
        hotel,
        "HOTEL-AMOUNT-CONFLICT",
        {"amount": "680.00"},
        "HUMAN_REVIEW",
        [rule("RULE-LODGING-AMOUNT-LIMIT", "PASS", "EVID-DEPT-LODGING-LIMIT", hotel_invoice)],
    )
    variant(
        hotel,
        "HOTEL-ROOM-CONFLICT",
        {"room_count": 2},
        "HUMAN_REVIEW",
        [rule("RULE-LODGING-AMOUNT-LIMIT", "PASS", "EVID-DEPT-LODGING-LIMIT", hotel_invoice)],
    )
    variant(
        hotel,
        "HOTEL-CITY-MISSING",
        {"city": None},
        "HUMAN_REVIEW",
        [rule("RULE-LODGING-AMOUNT-LIMIT", "INDETERMINATE", "EVID-DEPT-LODGING-LIMIT")],
    )

    template = retrieval[0]
    queries = (
        (
            "SALES-260",
            "销售人员拜访客户打车 275 元，单次额度和票据材料分别怎么要求？",
            "DEPT-SALES",
            "交通",
            "2026-04-18",
            ("EVID-SALES-TAXI-LIMIT", "EVID-SALES-TAXI-MATERIALS"),
        ),
        (
            "SALES-RECEIPT",
            "销售部出租车报销需要哪种票据和行程材料？",
            "DEPT-SALES",
            "交通",
            "2026-04-18",
            "EVID-SALES-TAXI-MATERIALS",
        ),
        (
            "SALES-ROUTE",
            "销售员工从家到固定办公地点的出租车费能报销吗？",
            "DEPT-SALES",
            "交通",
            "2026-04-20",
            "EVID-SALES-COMMUTE-BAN",
        ),
        (
            "SALES-COMMUTE-CLAIM",
            "票据写住所到固定办公室，但事由写客户拜访，通勤限制适用吗？",
            "DEPT-SALES",
            "交通",
            "2026-04-20",
            "EVID-SALES-COMMUTE-BAN",
        ),
        (
            "OPS-TAXI-NET",
            "运营部网约车优惠后的员工实付金额如何与限额比较？",
            "DEPT-OPERATIONS",
            "交通",
            "2026-05-20",
            "EVID-DEPT-TRANSPORT-RULE",
        ),
        (
            "OPS-NO-RECEIPT",
            "运营部交通费没有原始票据时可直接自动通过吗？",
            "DEPT-OPERATIONS",
            "交通",
            "2026-05-20",
            "EVID-DEPT-TRANSPORT-MISSING",
        ),
        (
            "OPS-LATE",
            "运营部费用超过提交时限，需要怎样处理？",
            "DEPT-OPERATIONS",
            "交通",
            "2026-05-20",
            "EVID-DEPT-DEADLINE",
        ),
        (
            "MEAL-PER-PERSON",
            "市场部工作餐每位参加者的限额是多少？",
            "DEPT-MARKETING",
            "餐饮",
            "2026-05-20",
            "EVID-DEPT-MEAL-LIMIT",
        ),
        (
            "MEAL-FACE-AMOUNT",
            "餐饮报销金额与发票票面金额不一致如何核对？",
            "DEPT-MARKETING",
            "餐饮",
            "2026-05-20",
            "EVID-DEPT-MEAL-CONSISTENCY",
        ),
        (
            "MEAL-NO-COUNT",
            "市场部餐饮缺少实际参加人数还能算人均上限吗？",
            "DEPT-MARKETING",
            "餐饮",
            "2026-05-20",
            "EVID-DEPT-MEAL-LIMIT",
        ),
        (
            "HOTEL-A-LIMIT",
            "运营部在 A 类城市住宿每间夜适用什么标准？",
            "DEPT-OPERATIONS",
            "住宿",
            "2026-05-24",
            "EVID-DEPT-LODGING-LIMIT",
        ),
        (
            "HOTEL-EXCEPTION",
            "会议酒店住宿 740 元，先怎么算正常额度，超出后需要什么例外审批？",
            "DEPT-OPERATIONS",
            "住宿",
            "2026-05-24",
            ("EVID-DEPT-LODGING-LIMIT", "EVID-DEPT-LODGING-EXCEPTION"),
        ),
        (
            "HOTEL-NIGHTS",
            "住宿费核算要怎样按房间数和入住夜数计算？",
            "DEPT-OPERATIONS",
            "住宿",
            "2026-05-24",
            "EVID-DEPT-LODGING-LIMIT",
        ),
        (
            "NO-ANSWER-CLOUD",
            "个人云盘会员按住宿费用能报销多少？",
            "DEPT-OPERATIONS",
            "住宿",
            "2026-05-24",
            None,
        ),
        (
            "NO-ANSWER-PET",
            "私人宠物护理是否有业务餐饮的人均额度？",
            "DEPT-MARKETING",
            "餐饮",
            "2026-05-20",
            None,
        ),
        (
            "NO-ANSWER-GAME",
            "个人游戏订阅能按公务交通票据核销吗？",
            "DEPT-SALES",
            "交通",
            "2026-04-18",
            None,
        ),
        (
            "NO-ANSWER-MEDICAL",
            "个人体检费在住宿制度里每晚限额是多少？",
            "DEPT-OPERATIONS",
            "住宿",
            "2026-05-24",
            None,
        ),
    )
    for slug, query, department, expense, effective_at, evidence_id in queries:
        evidence_ids = (
            evidence_id if isinstance(evidence_id, tuple) else (evidence_id,) if evidence_id else ()
        )
        row = deepcopy(template)
        row.pop("case_id", None)
        row.update(
            sample_id=f"RET-VAL-{slug}",
            dataset_version=VERSION,
            leakage_group_id=f"LG-VAL-QUERY-{slug}",
            clause_family_id=slug,
            query_family_id=f"QF-VAL-{slug}",
            query=query,
            filters={
                "expense_type": expense,
                "effective_at": effective_at,
                "department_id": department,
            },
            no_answer=not evidence_ids,
            relevant_evidence_groups=[
                {"group_id": f"RG-VAL-{slug}-{index}", "any_of": [item], "relevance": 3}
                for index, item in enumerate(evidence_ids, 1)
            ],
        )
        retrieval.append(row)

    attachments, invoice_evidence, attachment_chunks = independent_invoices()
    add_independent_cases(approvals, trajectories)

    # Use the existing PDF/evidence validator against an in-memory draft bundle.
    bundle, manifest, structured, ref_structured, catalog, errors = read_bundle(ROOT)
    manifest["attachments"].extend(attachments)
    catalog["items"].extend(invoice_evidence)
    for items in bundle.values():
        for item in items:
            item["dataset_version"] = VERSION
    bundle.update(
        approvals=[
            *([row for row in bundle["approvals"] if row["split"] != "validation"]),
            *approvals,
        ],
        agent_trajectories=[
            *([row for row in bundle["agent_trajectories"] if row["split"] != "validation"]),
            *trajectories,
        ],
        retrieval=[
            *([row for row in bundle["retrieval"] if row["split"] != "validation"]),
            *retrieval,
        ],
    )
    errors.extend(validate_bundle(ROOT, bundle, manifest, structured, ref_structured, catalog))
    if errors:
        raise ValueError("Draft validation failed:\n" + "\n".join(errors))

    TARGET.mkdir(parents=True, exist_ok=True)
    for name, items in (
        ("approvals", approvals),
        ("agent_trajectories", trajectories),
        ("retrieval", retrieval),
    ):
        (TARGET / f"{name}.jsonl").write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in items
            ),
            encoding="utf-8",
        )
    (TARGET / "attachment_manifest.json").write_text(
        json.dumps(
            {"dataset_version": VERSION, "attachments": attachments}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    (TARGET / "attachment_evidence.json").write_text(
        json.dumps(
            {"dataset_version": VERSION, "items": invoice_evidence}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    (TARGET / "attachment_chunks.jsonl").write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            for item in attachment_chunks
        ),
        encoding="utf-8",
    )
    print(
        f"Draft validation: approvals={len(approvals)}, trajectories={len(trajectories)}, "
        f"retrieval={len(retrieval)}; frozen test unchanged"
    )


if __name__ == "__main__":
    main()
