"""Build the corrected 1.5.1 validation matrix from the 1.5 invoice fixtures."""

from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingestion.models import ParsedBlock
from app.ingestion.pipeline import canonicalize_and_chunk
from scripts.build_fixture_pdfs import FONT, build_pdf
from scripts.build_validation_draft import rows, rule
from scripts.validate_datasets import read_bundle, validate_bundle

BASE = ROOT / "evals/datasets/draft_1_4"
INVOICE_FOLDER = ROOT / "evals/datasets/draft_1_5/attachments"
TARGET = ROOT / "evals/datasets/draft_1_5_2"
VERSION = "1.5.2"
HOLDOUT_VERSION = "1.5.0"


def meal(
    slug: str,
    day: int,
    amount: str,
    count: int,
    description: str,
    *,
    department: str = "DEPT-MARKETING",
    outcome: str = "PASS",
    application_amount: str | None = None,
    application_date: str | None = None,
    application_count: int | None = -1,
) -> dict:
    return {
        "slug": slug,
        "kind": "MEAL",
        "day": day,
        "amount": amount,
        "count": count,
        "description": description,
        "department": department,
        "outcome": outcome,
        "application_amount": application_amount,
        "application_date": application_date,
        "application_count": application_count,
    }


def taxi(
    slug: str,
    day: int,
    amount: str,
    route: str | None,
    description: str,
    *,
    department: str = "DEPT-OPERATIONS",
    outcome: str = "PASS",
    application_amount: str | None = None,
    application_date: str | None = None,
    late: bool = False,
) -> dict:
    return {
        "slug": slug,
        "kind": "TAXI",
        "day": day,
        "amount": amount,
        "route": route,
        "description": description,
        "department": department,
        "outcome": outcome,
        "application_amount": application_amount,
        "application_date": application_date,
        "late": late,
    }


# Twenty separate transactions. The fourteen extra rows below are explicitly counterfactuals,
# not fourteen more independent invoices.
INVOICES = (
    meal("M01", 1, "118.00", 1, "供应商交付计划工作餐"),
    meal("M02", 2, "320.00", 2, "渠道合作季度复盘餐叙"),
    meal("M03", 3, "216.00", 2, "客户现场验收后的简餐", department="DEPT-OPERATIONS"),
    meal("M04", 4, "185.00", 1, "产品演示后客户工作餐", outcome="REJECT"),
    meal("M05", 5, "348.00", 2, "区域合作方沟通工作餐", outcome="REJECT"),
    meal(
        "M06", 6, "145.00", 1, "合作协议讨论工作餐", outcome="REJECT", application_amount="155.00"
    ),
    meal(
        "M07", 7, "138.00", 1, "项目启动会后工作餐", outcome="REJECT", application_date="2026-06-08"
    ),
    meal(
        "M08",
        8,
        "242.00",
        2,
        "服务点位巡检工作餐",
        department="DEPT-OPERATIONS",
        outcome="HUMAN",
        application_count=3,
    ),
    meal("M09", 9, "285.00", 2, "联合营销活动工作餐", outcome="HUMAN", application_count=None),
    meal("M10", 10, "160.00", 1, "客户方案确认工作餐"),
    taxi("T01", 11, "86.00", "研发园区至客户培训中心", "培训设备搬运协调"),
    taxi("T02", 12, "300.00", "会展北门至合作园区", "展会布展现场支持"),
    taxi("T03", 13, "228.00", "服务中心至工业园东门", "客户设备复核"),
    taxi("T04", 14, "326.00", "酒店会议中心至客户园区", "技术交流交通", outcome="REJECT"),
    taxi("T05", 15, "307.00", "合作方仓库至售后站点", "物料交接交通", outcome="REJECT"),
    taxi(
        "T06",
        16,
        "126.00",
        "测试基地至客户大楼",
        "现场故障排查",
        outcome="REJECT",
        application_amount="116.00",
    ),
    taxi(
        "T07",
        17,
        "174.00",
        "会展中心至联络办公室",
        "活动收尾协调",
        outcome="REJECT",
        application_date="2026-06-18",
    ),
    taxi("T08", 18, "98.00", "服务网点至客户数据中心", "设备巡检交通", outcome="HUMAN", late=True),
    taxi(
        "T09",
        19,
        "265.00",
        "客户研发楼至项目指挥部",
        "联合测试现场支持",
        department="DEPT-MARKETING",
    ),
    taxi("T10", 20, "72.00", None, "外勤交通，票据未列明起讫地点", outcome="HUMAN"),
)

# These candidate inputs deliberately carry no recommendation gold. A reviewer must label
# them before a future version can be frozen as test.
HOLDOUT = (
    {**meal("HM01", 1, "92.00", 1, "合作项目需求澄清午餐", outcome="UNLABELED"), "month": 7},
    {**meal("HM02", 2, "160.00", 1, "客户现场培训工作餐", outcome="UNLABELED"), "month": 7},
    {**meal("HM03", 3, "176.00", 1, "售后问题复盘餐叙", outcome="UNLABELED"), "month": 7},
    {**meal("HM04", 4, "304.00", 2, "联合交付计划餐会", outcome="UNLABELED"), "month": 7},
    {**meal("HM05", 5, "356.00", 2, "渠道签约准备工作餐", outcome="UNLABELED"), "month": 7},
    {
        **meal(
            "HM06",
            6,
            "136.00",
            1,
            "年度服务回访餐叙",
            outcome="UNLABELED",
            application_amount="146.00",
        ),
        "month": 7,
    },
    {
        **meal(
            "HM07",
            7,
            "268.00",
            2,
            "设备验收协调工作餐",
            outcome="UNLABELED",
            application_count=3,
            department="DEPT-OPERATIONS",
        ),
        "month": 7,
    },
    {
        **meal(
            "HM08", 8, "225.00", 2, "项目驻场工作餐", outcome="UNLABELED", application_count=None
        ),
        "month": 7,
    },
    {
        **taxi("HT01", 9, "54.00", "项目园区至培训站", "设备培训交通", outcome="UNLABELED"),
        "month": 7,
    },
    {
        **taxi("HT02", 10, "299.00", "临港展厅至客户实验室", "产品联合测试", outcome="UNLABELED"),
        "month": 7,
    },
    {
        **taxi("HT03", 11, "312.00", "售后仓储点至会议中心", "会议材料交接", outcome="UNLABELED"),
        "month": 7,
    },
    {
        **taxi(
            "HT04",
            12,
            "132.00",
            "项目驻地至客户机房",
            "网络设备核验",
            outcome="UNLABELED",
            application_amount="142.00",
        ),
        "month": 7,
    },
    {
        **taxi(
            "HT05",
            13,
            "184.00",
            "合作园区至数据中心",
            "服务等级评审",
            outcome="UNLABELED",
            application_date="2026-07-14",
        ),
        "month": 7,
    },
    {
        **taxi("HT06", 14, "66.00", None, "临时外勤交通，行程信息缺失", outcome="UNLABELED"),
        "month": 7,
    },
    {
        **taxi(
            "HT07",
            15,
            "126.00",
            "展馆南门至供应商办公室",
            "联合演示准备",
            outcome="UNLABELED",
            late=True,
        ),
        "month": 7,
    },
    {
        **taxi("HT08", 16, "238.00", "服务站至客户样机库", "样机复检交通", outcome="UNLABELED"),
        "month": 7,
    },
    *(
        {
            "slug": f"HH{index:02d}",
            "kind": "HOTEL",
            "month": 7,
            "day": 16 + index,
            "amount": amount,
            "city": city,
            "rooms": rooms,
            "description": description,
            "department": "DEPT-OPERATIONS",
            "outcome": "UNLABELED",
            "application_amount": application_amount,
        }
        for index, amount, city, rooms, description, application_amount in (
            (1, "590.00", "杭州市", 1, "客户联合会议住宿", None),
            (2, "742.00", "杭州市", 1, "主办方指定会议酒店", None),
            (3, "538.00", "苏州市", 1, "驻场交付住宿", None),
            (4, "480.00", "宁波市", 1, "供应商验收住宿", "500.00"),
        )
    ),
)


def document_text(item: dict, *, prefix: str) -> str:
    slug = item["slug"]
    kind = item["kind"]
    layout = ("TABLE", "RECEIPT", "LETTER")[(item["day"] - 1) % 3]
    heading = {"MEAL": "业务餐饮电子票据", "TAXI": "公务出行费用凭证", "HOTEL": "住宿费用电子发票"}[
        kind
    ]
    source = "市场业务中心" if item["department"] == "DEPT-MARKETING" else "运营服务中心"
    date_text = f"2026-{item.get('month', 6):02d}-{item['day']:02d}"
    details = (
        [
            f"用餐事项：{item['description']}",
            f"实际参加人数：{item['count']} 人",
            f"票面金额：{item['amount']} 元",
        ]
        if kind == "MEAL"
        else [
            *([f"路线：{item['route']}"] if item["route"] else []),
            f"业务目的：{item['description']}",
            f"员工实际支付：{item['amount']} 元",
        ]
        if kind == "TAXI"
        else [
            f"城市：{item['city']}",
            f"{date_text} 入住",
            f"2026-07-{item['day'] + 1:02d} 退房",
            f"{item['rooms']} 间客房",
            f"住宿事项：{item['description']}",
            f"票面金额：{item['amount']} 元",
        ]
    )
    core = [f"票据号：SYN-{prefix}-{slug}", f"开票日期：{date_text}", *details]
    if layout == "TABLE":
        body = [
            f"# {heading}",
            "| 凭证项目 | 记录内容 |",
            "| --- | --- |",
            f"| 使用部门 | {source} |",
            f"| 费用场景 | {item['description']} |",
            "",
            "## 核验字段",
            *core,
        ]
    elif layout == "RECEIPT":
        body = [f"# {heading}", f"经办单位：{source}", "## 电子票据信息", *core]
    else:
        body = [f"# {source}费用记录", f"## {heading}", "记录用途：业务报销核对", *core]
    return "\n".join(
        [
            "<!-- PAGE 1 -->",
            f"<!-- LAYOUT {layout} -->",
            *body,
            f"附件编号：DOC-{prefix}-{slug}",
            "本票据由项目合成，不代表真实交易。",
            "",
        ]
    )


def write_pdf(item: dict, folder: Path, *, prefix: str) -> tuple[dict, str]:
    document_id = f"DOC-{prefix}-{item['slug']}"
    source = folder / f"{document_id}.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(document_text(item, prefix=prefix), encoding="utf-8")
    pdf = build_pdf(source)
    reader = PdfReader(pdf)
    page_text = reader.pages[0].extract_text()
    if len(reader.pages) != 1 or f"SYN-{prefix}-{item['slug']}" not in page_text:
        raise ValueError(f"unreadable authored invoice: {document_id}")
    metadata = {
        "document_id": document_id,
        "fixture_key": f"SYNTHETIC_{prefix}_{item['slug']}",
        "document_type": {"MEAL": "MEAL_INVOICE", "TAXI": "TAXI_INVOICE", "HOTEL": "HOTEL_INVOICE"}[
            item["kind"]
        ],
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
    return metadata, page_text


def invoice_fields(item: dict, document_id: str, *, prefix: str) -> list[dict]:
    values = [
        ("invoice_number", f"SYN-{prefix}-{item['slug']}"),
        ("amount", item["amount"]),
        ("occurred_on", f"2026-06-{item['day']:02d}"),
    ]
    if item["kind"] == "MEAL":
        values.append(("attendee_count", item["count"]))
    if item["kind"] == "TAXI" and item["route"]:
        values.append(("route", f"路线：{item['route']}"))
    return [
        {
            "field": field,
            "status": "PRESENT",
            "value": value,
            "raw_value": str(value),
            "document_id": document_id,
            "page": 1,
        }
        for field, value in values
    ]


def new_case(item: dict, base: dict, *, prefix: str = "V15") -> tuple[dict, dict]:
    slug = item["slug"]
    is_meal = item["kind"] == "MEAL"
    document_id = f"DOC-{prefix}-{slug}"
    evidence_id = f"EVID-{prefix}-{slug}-INVOICE"
    policy_id = "EVID-DEPT-MEAL-LIMIT" if is_meal else "EVID-DEPT-TRANSPORT-RULE"
    invoice_date = date(2026, 6, item["day"])
    amount = item["application_amount"] or item["amount"]
    occurred_on = item["application_date"] or invoice_date.isoformat()
    application = {
        "request_id": f"REQ-{prefix}-{slug}",
        "expense_type": "餐饮" if is_meal else "交通",
        "currency": "CNY",
        "amount": amount,
        "occurred_on": occurred_on,
        "submitted_on": (invoice_date + timedelta(days=50 if item.get("late") else 1)).isoformat(),
        "description": item["description"] + (f"：{item['route']}" if item.get("route") else ""),
    }
    if is_meal:
        application["attendee_count"] = (
            item["count"] if item["application_count"] == -1 else item["application_count"]
        )
    recommendation = {
        "PASS": "PASS_RECOMMENDED",
        "REJECT": "REJECT_RECOMMENDED",
        "HUMAN": "HUMAN_REVIEW",
    }[item["outcome"]]
    policy_ids = [policy_id]
    if item.get("late"):
        policy_ids.append("EVID-DEPT-DEADLINE")
    if not is_meal and not item.get("route"):
        policy_ids.append("EVID-DEPT-TRANSPORT-MISSING")
    if is_meal:
        mismatch = amount != item["amount"] or occurred_on != invoice_date.isoformat()
        consistency = "FAIL" if mismatch else "PASS"
        count = application["attendee_count"]
        limit = (
            "INDETERMINATE" if count is None else "PASS" if float(amount) <= 160 * count else "FAIL"
        )
        rules = [
            rule("RULE-MEAL-DOCUMENT-CONSISTENCY", consistency, evidence_id),
            rule("RULE-OPS-MKT-MEAL-LIMIT", limit, policy_id),
        ]
    else:
        consistency = (
            "INDETERMINATE"
            if not item.get("route")
            else "FAIL"
            if amount != item["amount"] or occurred_on != invoice_date.isoformat()
            else "PASS"
        )
        limit = "PASS" if float(amount) <= 300 else "FAIL"
        rules = [
            rule("RULE-AMOUNT-DATE-CONSISTENCY", consistency, evidence_id),
            rule("RULE-OPS-MKT-TRANSPORT-LIMIT", limit, policy_id, evidence_id),
        ]
        if item.get("late"):
            rules.append(rule("RULE-SUBMISSION-DEADLINE", "FAIL", "EVID-DEPT-DEADLINE"))
    stop = (
        ["EVIDENCE_SUFFICIENT"]
        if item["outcome"] == "PASS"
        else ["CLEAR_RULE_FAILURE", "EVIDENCE_SUFFICIENT"]
        if item["outcome"] == "REJECT"
        else ["MATERIALS_UNAVAILABLE", "EVIDENCE_ESCALATION", "MANDATORY_HUMAN_EXCEPTION"]
    )
    identity = {
        key: deepcopy(base[key])
        for key in (
            "source_type",
            "synthesis_method",
            "license_id",
            "license_scope",
            "reference_source_ids",
            "policy_catalog_snapshot_id",
        )
    }
    identity.update(
        dataset_version=VERSION,
        split="validation",
        leakage_group_id=f"LG-VAL-{prefix}-{slug}",
        clause_family_id="OPS-MKT-MEAL-LIMIT" if is_meal else "OPS-MKT-TRANSPORT-LIMIT",
        case_id=f"CASE-VAL-{prefix}-{slug}",
    )
    approval = {
        **identity,
        "sample_id": f"APP-VAL-{prefix}-{slug}",
        "independence_class": "NEW_INVOICE",
        "input": {
            "applicant": {
                "employee_id": f"EMP-{prefix}-{slug}",
                "department_id": item["department"],
                "display_name": "合成员工",
            },
            "application": application,
            "documents": [
                {
                    "document_id": document_id,
                    "fixture_key": f"SYNTHETIC_{prefix}_{slug}",
                    "document_type": "MEAL_INVOICE" if is_meal else "TAXI_INVOICE",
                    "media_type": "application/pdf",
                    "synthetic": True,
                }
            ],
        },
        "expected_extracted_fields": invoice_fields(item, document_id, prefix=prefix),
        "expected_evidence_ids": [*policy_ids, evidence_id],
        "expected_rule_results": rules,
        "expected_risk_flags": ["OVERDUE_EXCEPTION_REQUIRED"] if item.get("late") else [],
        "expected_recommendation": recommendation,
        "acceptable_stop_reasons": stop,
    }
    trajectory = {
        **identity,
        "sample_id": f"TRAJ-VAL-{prefix}-{slug}",
        "order_independent": True,
        "allowed_tools": ["policy_search"],
        "hard_limits": {"max_agent_steps": 5, "max_retrieval_rounds": 3, "max_query_rewrites": 2},
        "required_sub_questions": [
            {
                "intent": "APPLICABLE_POLICY_BOUNDARY",
                "text": "核对适用费用标准及票据材料。",
                "required_evidence_ids": policy_ids,
            }
        ],
        "required_tool_capabilities": [
            {
                "selected_tool_name": "policy_search",
                "purpose": "FIND_APPLICABLE_POLICY",
                "query_type": "",
            }
        ],
        "required_evidence_ids": [*policy_ids, evidence_id],
        "acceptable_stop_reasons": stop,
        "forbidden_actions": ["FABRICATE_EVIDENCE", "ISSUE_FINAL_APPROVAL"],
    }
    return approval, trajectory


def write_jsonl(path: Path, items: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in items),
        encoding="utf-8",
    )


def build_holdout_candidates() -> None:
    folder = ROOT / "evals/datasets/holdout_candidates_1_5"
    documents: list[dict] = []
    candidates: list[dict] = []
    for item in HOLDOUT:
        metadata, _ = write_pdf(item, folder / "attachments", prefix="HC15")
        documents.append(metadata)
        invoice_date = date(2026, 7, item["day"])
        application = {
            "request_id": f"REQ-HC15-{item['slug']}",
            "expense_type": {"MEAL": "餐饮", "TAXI": "交通", "HOTEL": "住宿"}[item["kind"]],
            "currency": "CNY",
            "amount": item.get("application_amount") or item["amount"],
            "occurred_on": item.get("application_date") or invoice_date.isoformat(),
            "submitted_on": (
                invoice_date + timedelta(days=50 if item.get("late") else 1)
            ).isoformat(),
            "description": item["description"]
            + (f"：{item['route']}" if item.get("route") else ""),
        }
        if item["kind"] == "MEAL":
            application["attendee_count"] = (
                item["count"] if item["application_count"] == -1 else item["application_count"]
            )
        elif item["kind"] == "HOTEL":
            application.update(
                city=item["city"],
                check_in=invoice_date.isoformat(),
                check_out=(invoice_date + timedelta(days=1)).isoformat(),
                room_count=item["rooms"],
            )
        candidates.append(
            {
                "candidate_id": f"HC15-{item['slug']}",
                "candidate_split": "test",
                "review_status": "UNLABELED",
                "leakage_group_id": f"LG-HC15-{item['slug']}",
                "policy_catalog_snapshot_id": "POLICY-CATALOG-REF-1.2.0",
                "structured_fixture_status": (
                    "CITY_TIER_AND_FACTS_NOT_SEEDED"
                    if item["kind"] == "HOTEL"
                    else "NEW_FACT_FIXTURES_NOT_SEEDED"
                ),
                "input": {
                    "applicant": {
                        "employee_id": f"EMP-HC15-{item['slug']}",
                        "department_id": item["department"],
                        "display_name": "合成员工",
                    },
                    "application": application,
                    "documents": [
                        {
                            "document_id": metadata["document_id"],
                            "fixture_key": metadata["fixture_key"],
                            "document_type": metadata["document_type"],
                            "media_type": "application/pdf",
                            "synthetic": True,
                        }
                    ],
                },
            }
        )
    folder.mkdir(parents=True, exist_ok=True)
    write_jsonl(folder / "candidates.jsonl", candidates)
    (folder / "attachment_manifest.json").write_text(
        json.dumps(
            {"candidate_version": HOLDOUT_VERSION, "attachments": documents}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    pdfmetrics.registerFont(UnicodeCIDFont(FONT))
    approvals = [
        {**deepcopy(row), "dataset_version": VERSION} for row in rows(BASE / "approvals.jsonl")
    ]
    # These two archived meal labels require structured records that the active
    # department-meal workflow neither queries nor uses for its rule decision.
    for row in approvals:
        if row["sample_id"] in {"APP-VAL-MEAL-PASS", "APP-VAL-MEAL-MISMATCH"}:
            row["expected_evidence_ids"] = [
                evidence_id for evidence_id in row["expected_evidence_ids"]
                if not evidence_id.startswith("EVID-STRUCT-")
            ]
    trajectories = [
        {**deepcopy(row), "dataset_version": VERSION}
        for row in rows(BASE / "agent_trajectories.jsonl")
    ]
    retrieval = [
        {**deepcopy(row), "dataset_version": VERSION} for row in rows(BASE / "retrieval.jsonl")
    ]
    attachment_manifest = json.loads(
        (BASE / "attachment_manifest.json").read_text(encoding="utf-8")
    )
    evidence_catalog = json.loads((BASE / "attachment_evidence.json").read_text(encoding="utf-8"))
    chunks = rows(BASE / "attachment_chunks.jsonl")

    for item in INVOICES:
        metadata, text = write_pdf(item, INVOICE_FOLDER, prefix="V15")
        attachment_manifest["attachments"].append(metadata)
        evidence_catalog["items"].append(
            {
                "evidence_id": f"EVID-V15-{item['slug']}-INVOICE",
                "source_type": "ATTACHMENT",
                "document_id": metadata["document_id"],
                "version": "1",
                "page": 1,
                "excerpt": text.strip(),
            }
        )
        _, parsed = canonicalize_and_chunk(
            [ParsedBlock(page_idx=0, block_index=0, kind="text", text=text)],
            metadata,
            parse_artifact_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
        chunks.extend(chunk.model_dump(mode="json") for chunk in parsed)
        approval, trajectory = new_case(item, approvals[0])
        approvals.append(approval)
        trajectories.append(trajectory)

    # Same-invoice counterfactuals probe boundary behaviour; keep the same leakage group.
    variants = (
        (
            "M01",
            "AMOUNT-WRONG",
            {"amount": "128.00"},
            "REJECT",
            [rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "FAIL", "EVID-V15-M01-INVOICE")],
        ),
        (
            "M02",
            "COUNT-WRONG",
            {"attendee_count": 3},
            "HUMAN",
            [rule("RULE-OPS-MKT-MEAL-LIMIT", "PASS", "EVID-DEPT-MEAL-LIMIT")],
        ),
        (
            "M03",
            "DATE-WRONG",
            {"occurred_on": "2026-06-04"},
            "REJECT",
            [rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "FAIL", "EVID-V15-M03-INVOICE")],
        ),
        (
            "M04",
            "COUNT-MISSING",
            {"attendee_count": None},
            "HUMAN",
            [rule("RULE-OPS-MKT-MEAL-LIMIT", "INDETERMINATE", "EVID-DEPT-MEAL-LIMIT")],
        ),
        (
            "M05",
            "AMOUNT-WRONG",
            {"amount": "320.00"},
            "REJECT",
            [rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "FAIL", "EVID-V15-M05-INVOICE")],
        ),
        (
            "M06",
            "AMOUNT-CORRECTED",
            {"amount": "145.00"},
            "PASS",
            [
                rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "PASS", "EVID-V15-M06-INVOICE"),
                rule("RULE-OPS-MKT-MEAL-LIMIT", "PASS", "EVID-DEPT-MEAL-LIMIT"),
            ],
        ),
        (
            "M07",
            "DATE-CORRECTED",
            {"occurred_on": "2026-06-07"},
            "PASS",
            [
                rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "PASS", "EVID-V15-M07-INVOICE"),
                rule("RULE-OPS-MKT-MEAL-LIMIT", "PASS", "EVID-DEPT-MEAL-LIMIT"),
            ],
        ),
        (
            "M08",
            "COUNT-CORRECTED",
            {"attendee_count": 2},
            "PASS",
            [
                rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "PASS", "EVID-V15-M08-INVOICE"),
                rule("RULE-OPS-MKT-MEAL-LIMIT", "PASS", "EVID-DEPT-MEAL-LIMIT"),
            ],
        ),
        (
            "M09",
            "COUNT-SUPPLIED",
            {"attendee_count": 2},
            "PASS",
            [
                rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "PASS", "EVID-V15-M09-INVOICE"),
                rule("RULE-OPS-MKT-MEAL-LIMIT", "PASS", "EVID-DEPT-MEAL-LIMIT"),
            ],
        ),
        (
            "T01",
            "AMOUNT-WRONG",
            {"amount": "96.00"},
            "REJECT",
            [rule("RULE-AMOUNT-DATE-CONSISTENCY", "FAIL", "EVID-V15-T01-INVOICE")],
        ),
        (
            "T02",
            "DATE-WRONG",
            {"occurred_on": "2026-06-13"},
            "REJECT",
            [rule("RULE-AMOUNT-DATE-CONSISTENCY", "FAIL", "EVID-V15-T02-INVOICE")],
        ),
        (
            "T04",
            "AMOUNT-WRONG",
            {"amount": "300.00"},
            "REJECT",
            [rule("RULE-AMOUNT-DATE-CONSISTENCY", "FAIL", "EVID-V15-T04-INVOICE")],
        ),
        (
            "T06",
            "AMOUNT-CORRECTED",
            {"amount": "126.00"},
            "PASS",
            [
                rule("RULE-AMOUNT-DATE-CONSISTENCY", "PASS", "EVID-V15-T06-INVOICE"),
                rule(
                    "RULE-OPS-MKT-TRANSPORT-LIMIT",
                    "PASS",
                    "EVID-DEPT-TRANSPORT-RULE",
                    "EVID-V15-T06-INVOICE",
                ),
            ],
        ),
        (
            "T07",
            "DATE-CORRECTED",
            {"occurred_on": "2026-06-17"},
            "PASS",
            [
                rule("RULE-AMOUNT-DATE-CONSISTENCY", "PASS", "EVID-V15-T07-INVOICE"),
                rule(
                    "RULE-OPS-MKT-TRANSPORT-LIMIT",
                    "PASS",
                    "EVID-DEPT-TRANSPORT-RULE",
                    "EVID-V15-T07-INVOICE",
                ),
            ],
        ),
    )
    by_slug = {
        row["sample_id"].removeprefix("APP-VAL-V15-"): row
        for row in approvals
        if row["sample_id"].startswith("APP-VAL-V15-")
    }
    by_case = {row["case_id"]: row for row in trajectories}
    for slug, variant, changes, outcome, expected_rules in variants:
        parent = by_slug[slug]
        approval = deepcopy(parent)
        approval.update(
            sample_id=f"APP-VAL-V15-{slug}-{variant}",
            case_id=f"CASE-VAL-V15-{slug}-{variant}",
            independence_class="SAME_INVOICE_COUNTERFACTUAL",
            expected_rule_results=expected_rules,
            expected_recommendation={
                "PASS": "PASS_RECOMMENDED",
                "REJECT": "REJECT_RECOMMENDED",
                "HUMAN": "HUMAN_REVIEW",
            }[outcome],
            acceptable_stop_reasons=(
                ["EVIDENCE_SUFFICIENT"]
                if outcome == "PASS"
                else ["CLEAR_RULE_FAILURE", "EVIDENCE_SUFFICIENT"]
                if outcome == "REJECT"
                else ["MATERIALS_UNAVAILABLE", "EVIDENCE_ESCALATION", "MANDATORY_HUMAN_EXCEPTION"]
            ),
            expected_risk_flags=[],
        )
        approval["input"]["application"].update(request_id=f"REQ-V15-{slug}-{variant}", **changes)
        approval["expected_evidence_ids"] = list(
            dict.fromkeys(
                [
                    *parent["expected_evidence_ids"],
                    *(eid for result in expected_rules for eid in result["evidence_ids"]),
                ]
            )
        )
        trajectory = deepcopy(by_case[parent["case_id"]])
        trajectory.update(
            sample_id=f"TRAJ-VAL-V15-{slug}-{variant}",
            case_id=approval["case_id"],
            acceptable_stop_reasons=approval["acceptable_stop_reasons"],
        )
        approvals.append(approval)
        trajectories.append(trajectory)

    # Human-labelled count conflicts and missing applicant counts must block auto-pass.
    count_gaps = {
        "APP-VAL-MEAL-NO-HEADCOUNT": ("EVID-MKT-MEAL-PASS-INVOICE", 1),
        "APP-VAL-MEAL-HEADCOUNT-CONFLICT": ("EVID-MKT-MEAL-PASS-INVOICE", 1),
        "APP-VAL-MEAL-SECOND-HEADCOUNT-CONFLICT": ("EVID-MKT-MEAL-MISMATCH-INVOICE", 1),
        "APP-VAL-V15-M08": ("EVID-V15-M08-INVOICE", 2),
        "APP-VAL-V15-M09": ("EVID-V15-M09-INVOICE", 2),
        "APP-VAL-V15-M02-COUNT-WRONG": ("EVID-V15-M02-INVOICE", 2),
        "APP-VAL-V15-M04-COUNT-MISSING": ("EVID-V15-M04-INVOICE", 1),
    }
    for approval in approvals:
        if approval["sample_id"] not in count_gaps:
            continue
        evidence_id, invoice_count = count_gaps[approval["sample_id"]]
        approval["expected_rule_results"] = [
            item
            for item in approval["expected_rule_results"]
            if item["rule_id"] != "RULE-MEAL-DOCUMENT-CONSISTENCY"
        ] + [rule("RULE-MEAL-DOCUMENT-CONSISTENCY", "INDETERMINATE", evidence_id)]
        approval["expected_evidence_ids"] = list(
            dict.fromkeys(
                [
                    *approval["expected_evidence_ids"],
                    evidence_id,
                ]
            )
        )
        if not any(
            item["field"] == "attendee_count" for item in approval["expected_extracted_fields"]
        ):
            approval["expected_extracted_fields"].append(
                {
                    "field": "attendee_count",
                    "status": "PRESENT",
                    "value": invoice_count,
                    "raw_value": str(invoice_count),
                    "document_id": approval["input"]["documents"][0]["document_id"],
                    "page": 1,
                }
            )

    attachment_manifest["dataset_version"] = VERSION
    evidence_catalog["dataset_version"] = VERSION
    bundle, manifest, structured, ref_structured, catalog, errors = read_bundle(ROOT)
    manifest["attachments"].extend(attachment_manifest["attachments"])
    catalog["items"].extend(evidence_catalog["items"])
    for group in bundle.values():
        for row in group:
            row["dataset_version"] = VERSION
    bundle["approvals"] = [
        row for row in bundle["approvals"] if row["split"] != "validation"
    ] + approvals
    bundle["agent_trajectories"] = [
        row for row in bundle["agent_trajectories"] if row["split"] != "validation"
    ] + trajectories
    bundle["retrieval"] = [
        row for row in bundle["retrieval"] if row["split"] != "validation"
    ] + retrieval
    errors.extend(validate_bundle(ROOT, bundle, manifest, structured, ref_structured, catalog))
    if errors:
        raise ValueError("Draft validation failed:\n" + "\n".join(errors))

    TARGET.mkdir(parents=True, exist_ok=True)
    for name, items in (
        ("approvals", approvals),
        ("agent_trajectories", trajectories),
        ("retrieval", retrieval),
        ("attachment_chunks", chunks),
    ):
        write_jsonl(TARGET / f"{name}.jsonl", items)
    for name, data in (
        ("attachment_manifest", attachment_manifest),
        ("attachment_evidence", evidence_catalog),
    ):
        (TARGET / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    build_holdout_candidates()
    print(
        f"{VERSION} draft: {len(approvals)} approval cases, {len(trajectories)} trajectories, "
        f"{len(attachment_manifest['attachments'])} newly authored PDFs in draft versions; "
        f"{len(HOLDOUT)} unlabeled holdout candidates; frozen test unchanged"
    )


if __name__ == "__main__":
    main()
