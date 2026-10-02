"""Author 40 harder synthetic cases and a 100-case validation bundle; no live LLM calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import DEFAULT_DATABASE_URL
from app.ingestion.models import ParsedBlock
from app.ingestion.pipeline import canonicalize_and_chunk
from scripts.build_fixture_pdfs import FONT, build_pdf
from scripts.build_validation_draft import rows, rule
from scripts.build_validation_expansion import write_jsonl

VERSION = "1.6.0"
BASE = ROOT / "evals/datasets/draft_1_5_2"
TARGET = ROOT / "evals/datasets/challenge_1_6"
COMBINED = ROOT / "evals/datasets/draft_1_6"
OPS = "DEPT-OPERATIONS"
MKT = "DEPT-MARKETING"
SALES = "DEPT-SALES"
RD = "DEPT-SYN-RD"
POLICY_SNAPSHOT = "POLICY-CATALOG-COMPANY-1.0.0"
POLICIES = {
    "HOTEL": "POL-LODGING-2026-V3",
    "OPS": "POL-DEPT-OPS-MKT-2026-V1",
    "SALES": "POL-DEPT-SALES-TRANSPORT-2026-V1",
    "TAXI": "POL-TRANSPORT-2026-V1",
    "OLD": "POL-DINING-2025-V1",
    "NEW": "POL-DINING-2026-V2",
}


def hotel(
    slug, difficulty, department, city, start, nights, rooms, amount, outcome, reason, **extra
):
    return dict(
        slug=slug,
        difficulty=difficulty,
        department=department,
        kind="HOTEL",
        city=city,
        start=start,
        nights=nights,
        rooms=rooms,
        amount=amount,
        outcome=outcome,
        reason=reason,
        description="客户系统部署驻场住宿",
        **extra,
    )


def meal(slug, difficulty, department, day, amount, count, outcome, reason, **extra):
    return dict(
        slug=slug,
        difficulty=difficulty,
        department=department,
        kind="MEAL",
        day=day,
        amount=amount,
        count=count,
        outcome=outcome,
        reason=reason,
        city="杭州市",
        description="客户接口联调后的业务工作餐，地点：杭州滨江客户园区餐厅",
        **extra,
    )


def taxi(slug, difficulty, department, day, amount, outcome, reason, **extra):
    return dict(
        slug=slug,
        difficulty=difficulty,
        department=department,
        kind="TAXI",
        day=day,
        amount=amount,
        outcome=outcome,
        reason=reason,
        description="客户现场设备验收交通",
        route="公司服务中心至客户项目园区",
        **extra,
    )


# Labels below are authored from policy clauses and arithmetic, not from workflow predictions.
# Each row is a new transaction; shared scenario families remain in validation together.
CASES = [
    hotel(
        "H01",
        "HIGH",
        RD,
        "常州市",
        "2026-05-13",
        2,
        1,
        "880.00",
        "PASS",
        "入住日城市等级仍为 B；不能按提交日的新 A 级追溯。实际入住水单补齐发票日期。",
        stay_confirmation=True,
    ),
    hotel(
        "H02",
        "MEDIUM",
        RD,
        "常州市",
        "2026-05-16",
        2,
        1,
        "1180.00",
        "PASS",
        "5 月 15 日调整已生效，A 级两间夜限额 1200；不是旧 B 级的 900。",
    ),
    hotel(
        "H03",
        "HIGH",
        OPS,
        "常州市",
        "2026-05-14",
        1,
        1,
        "500.00",
        "PASS",
        "发生日 B 级，运营部门标准 520 优先于通用 450；后来的 A 级不追溯。",
    ),
    hotel(
        "H04",
        "HIGH",
        OPS,
        "常州市",
        "2026-05-17",
        3,
        2,
        "4000.00",
        "PASS",
        "发票与实际水单合并核对两间房三晚，部门 A 级限额 680×3×2=4080。",
        stay_confirmation=True,
    ),
    hotel(
        "H05",
        "MEDIUM",
        RD,
        "常州市",
        "2026-05-18",
        2,
        2,
        "2440.00",
        "REJECT",
        "通用 A 级两房两晚限额 2400，超限 40；不能借用运营部门 680 标准。",
    ),
    hotel(
        "H06",
        "HIGH",
        OPS,
        "常州市",
        "2026-05-11",
        2,
        2,
        "2100.00",
        "REJECT",
        "调整前 B 级部门限额 520×2×2=2080；按后续 A 级会误通过。",
    ),
    hotel(
        "H07",
        "MEDIUM",
        RD,
        "杭州市",
        "2026-05-20",
        2,
        1,
        "1100.00",
        "HUMAN",
        "金额、住宿与城市记录齐全，但权威预算记录缺失，不能据申请人说明通过。",
        missing_budget=True,
        notes=[("预算情况说明", "部门经办人称预算充裕；尚未取得财务预算台账记录。")],
    ),
    hotel(
        "H08",
        "MEDIUM",
        OPS,
        "苏州市",
        "2026-05-21",
        2,
        1,
        "900.00",
        "HUMAN",
        "部门 B 级金额合规但预算数据缺失；额度合规不等于预算已核实。",
        missing_budget=True,
    ),
    hotel(
        "H09",
        "HIGH",
        RD,
        "合肥市",
        "2026-05-10",
        1,
        1,
        "500.00",
        "HUMAN",
        "分级记录 6 月 1 日才生效，发生日无等级；不得把未来记录当有效依据。",
        future_city=True,
    ),
    hotel(
        "H10",
        "MEDIUM",
        OPS,
        "杭州市",
        "2026-05-23",
        1,
        1,
        "600.00",
        "HUMAN",
        "部门限额内且日期一致，但查重记录为真，应暂停并人工核实重复申请。",
        duplicate=True,
    ),
    hotel(
        "H11",
        "MEDIUM",
        OPS,
        "长沙市",
        "2026-05-24",
        2,
        1,
        "1030.00",
        "PASS",
        "B 级部门限额 1040，预算恰好等于申请金额 1030；两个边界均满足。",
        exact_budget=True,
    ),
    hotel(
        "H12",
        "MEDIUM",
        RD,
        "重庆市",
        "2026-05-25",
        2,
        1,
        "1250.00",
        "REJECT",
        "两晚通用 A 级限额 1200，普通驻场超标；不能自行套会议指定酒店例外。",
    ),
    hotel(
        "H13",
        "HIGH",
        OPS,
        "杭州市",
        "2026-05-26",
        1,
        1,
        "760.00",
        "HUMAN",
        "会议指定酒店超标；通知、指定说明和部门确认齐全，但部门确认不是财务例外批准。",
        conference=True,
        notes=[
            (
                "会议通知与指定酒店说明",
                "客户技术会议于 2026-05-26 举行，主办方统一指定杭州滨江酒店，会议结束次日退房。",
            ),
            (
                "部门业务确认",
                "运营部门负责人确认会议出席必要性与酒店安排；本说明只确认业务事实，不是财务例外批准。",
            ),
        ],
    ),
    hotel(
        "H14",
        "HIGH",
        RD,
        "长沙市",
        "2026-05-27",
        1,
        1,
        "500.00",
        "HUMAN",
        "已有对应申请的财务书面例外材料，但超标例外仍交复核员作最终裁量，不能当正常限额内自动通过。",
        conference=True,
        notes=[
            (
                "会议通知与指定酒店说明",
                "设备交付会议于 2026-05-27 举行，主办方统一指定长沙会务酒店；住宿一晚。",
            ),
            (
                "财务例外处理记录",
                "财务负责人于 2026-05-26 书面同意 REQ-C16-H14 的指定酒店超标 50.00 元；部门已确认业务事实，仅限该申请及对应日期，不转用。",
            ),
        ],
    ),
    hotel(
        "H15",
        "MEDIUM",
        OPS,
        "杭州市",
        "2026-05-10",
        1,
        1,
        "600.00",
        "HUMAN",
        "从退房日到提交日 46 天，超过部门 45 天；部门延迟说明不等于正式时限例外。",
        submitted="2026-06-26",
        notes=[
            (
                "延迟提交说明",
                "部门负责人确认经办人较晚收到票据；未取得有权限人员的时限例外处理记录。",
            )
        ],
    ),
    hotel(
        "H16",
        "HIGH",
        RD,
        "常州市",
        "2026-05-14",
        1,
        1,
        "450.00",
        "PASS",
        "城市按入住日 B 级，提交按退房日起算；450 恰等于额度且提交恰为第 30 天。",
        submitted="2026-06-14",
    ),
    meal(
        "M01",
        "HIGH",
        RD,
        "2026-06-12",
        "270.00",
        2,
        "HUMAN",
        "重叠期人均 135：旧版不通过、新版通过；同权威且无替代关系，需要人工消歧。",
    ),
    meal(
        "M02",
        "HIGH",
        RD,
        "2026-06-13",
        "200.00",
        2,
        "PASS",
        "两版同时有效但人均 100 在两版都通过；标准不同不等于实质结论冲突。",
    ),
    meal(
        "M03",
        "HIGH",
        RD,
        "2026-06-15",
        "340.00",
        2,
        "REJECT",
        "人均 170 在两版都超限；无须先解决版本优先级才能建议驳回。",
    ),
    meal(
        "M04",
        "MEDIUM",
        RD,
        "2026-06-30",
        "135.00",
        1,
        "HUMAN",
        "旧版有效期末日仍生效；人均 135 使两版结论分歧，不能提前排除旧版。",
    ),
    meal(
        "M05",
        "MEDIUM",
        RD,
        "2026-07-01",
        "135.00",
        1,
        "PASS",
        "旧版刚失效，仅新版 150 上限适用；不能要求检索已失效的旧规则。",
    ),
    meal(
        "M06",
        "MEDIUM",
        RD,
        "2026-05-31",
        "130.00",
        1,
        "REJECT",
        "发生日仅旧版 120 有效，6 月 2 日提交不能套用新标准。",
        submitted="2026-06-02",
    ),
    meal(
        "M07",
        "HIGH",
        MKT,
        "2026-06-16",
        "300.00",
        2,
        "PASS",
        "人均 150 按部门 160 标准通过；通用新旧版本的重叠不应制造部门内冲突。",
    ),
    meal(
        "M08",
        "MEDIUM",
        OPS,
        "2026-06-18",
        "350.00",
        2,
        "REJECT",
        "部门上限 320，实际金额 350；业务必要性说明不能豁免人均超限。",
    ),
    meal(
        "M09",
        "MEDIUM",
        MKT,
        "2026-07-04",
        "315.00",
        2,
        "PASS",
        "人均 157.5 高于通用 150 但低于部门 160，必须按部门优先关系判断。",
    ),
    meal(
        "M10",
        "MEDIUM",
        OPS,
        "2026-07-05",
        "280.00",
        2,
        "HUMAN",
        "申请填写 3 人而原票据只有 2 人；两种人数下额度都合规也不能忽略事实冲突。",
        application_count=3,
    ),
    meal(
        "M11",
        "HIGH",
        OPS,
        "2026-06-20",
        "155.00",
        1,
        "PASS",
        "活动总预算 6200 已审批；5000 是活动预算门槛，不是本笔工作餐额度，155 按人均 160 核对。",
        notes=[
            (
                "活动预算批复",
                "客户联调活动总预算 6,200.00 元已完成事前申请与财务预算确认；其中本笔工作餐 155.00 元，杭州滨江客户园区餐厅，实际一人；不改变逐笔餐饮标准。",
            )
        ],
    ),
    meal(
        "M12",
        "HIGH",
        MKT,
        "2026-07-08",
        "240.00",
        2,
        "HUMAN",
        "原票据未记录人数，部门便笺不能代替商家明细；申请 2 人虽在额度内仍须补原始材料。",
        missing_count=True,
        notes=[
            ("经办部门便笺", "经办人称两名客户实际就餐；未提供商家消费明细或可核对的参加人员凭证。")
        ],
    ),
    taxi(
        "T01",
        "MEDIUM",
        SALES,
        "2026-04-02",
        "255.00",
        "PASS",
        "销售单次限额 260，票据、已确认客户拜访、预算、权限和查重均满足。",
        notes=[
            (
                "客户拜访登记回执",
                "2026-04-02 到客户项目园区开展设备验收，申请经办人与客户记录一致，记录已确认。",
            )
        ],
    ),
    taxi(
        "T02",
        "MEDIUM",
        SALES,
        "2026-04-03",
        "265.00",
        "REJECT",
        "原价 300 减优惠 35 后实付 265，仍超销售 260；不能借用运营 300。",
        payment_detail="订单原价：300.00 元；平台优惠：35.00 元；没有后续退款。",
    ),
    taxi(
        "T03",
        "MEDIUM",
        OPS,
        "2026-04-04",
        "270.00",
        "PASS",
        "原价 315 减优惠 45，实付 270 按运营 300 核对；不是按订单原价判断。",
        payment_detail="订单原价：315.00 元；平台优惠：45.00 元；没有后续退款。",
    ),
    taxi(
        "T04",
        "MEDIUM",
        OPS,
        "2026-04-05",
        "298.00",
        "REJECT",
        "票据最终实付 298，申请却填退款前 318；真实金额冲突，不能仅因 298 在限额内通过。",
        application_amount="318.00",
        payment_detail="订单原价：318.00 元；已退款：20.00 元；本票据显示最终结算金额。",
    ),
    taxi(
        "T05",
        "HIGH",
        SALES,
        "2026-04-06",
        "210.00",
        "HUMAN",
        "票据和客户拜访正常，但权威预算记录缺失；部门备忘录不能代替财务台账。",
        missing_budget=True,
        notes=[("部门预算备忘录", "销售经办人称预算充裕，尚无财务台账核对记录。")],
    ),
    taxi(
        "T06",
        "HIGH",
        SALES,
        "2026-04-07",
        "220.00",
        "HUMAN",
        "只有另一员工的交通权限记录；当前申请人权限未核实，不能跨员工借用授权。",
        missing_permission=True,
        notes=[("同事权限说明", "EMP-C16-OTHER 具有交通申请权限；此说明不代表当前申请人的权限。")],
    ),
    taxi(
        "T07",
        "HIGH",
        SALES,
        "2026-04-08",
        "120.00",
        "REJECT",
        "票据明确住所至固定办公地点，即使申请称客户拜访且存在拜访登记，也不能改写通勤性质。",
        commute=True,
        notes=[
            (
                "销售业务说明",
                "申请人称加班后需要到公司整理客户拜访材料；销售负责人确认客户事项，不批准通勤例外。",
            )
        ],
    ),
    taxi(
        "T08",
        "MEDIUM",
        RD,
        "2026-04-09",
        "168.00",
        "PASS",
        "住所到高铁站与已批准公务出差衔接，不是住所到固定办公地点；通用制度无此笔 260 上限。",
        station=True,
        notes=[
            (
                "公务出差行程确认",
                "已批准 2026-04-09 赴客户工厂出差，先由住所前往高铁站，再乘已预订高铁；非日常通勤。",
            )
        ],
    ),
    taxi(
        "T09",
        "HIGH",
        OPS,
        "2026-03-10",
        "180.00",
        "PASS",
        "提交第 43 天：超过通用 30 天但未超部门 45 天，应按运营补充细则。",
        submitted="2026-04-22",
    ),
    taxi(
        "T10",
        "HIGH",
        OPS,
        "2026-03-12",
        "190.00",
        "HUMAN",
        "提交第 49 天超部门 45 天；部门负责人延迟说明不是时限例外批准。",
        submitted="2026-04-30",
        notes=[
            (
                "部门延迟说明",
                "部门负责人确认经办人较晚交单；未取得财务时限例外处理记录，不改变金额标准。",
            )
        ],
    ),
    taxi(
        "T11",
        "HIGH",
        SALES,
        "2026-04-11",
        "230.00",
        "HUMAN",
        "权威查重为真，而经办便笺称未报销；不能用较低权威说明覆盖查重风险。",
        duplicate=True,
        notes=[("经办查重说明", "经办人认为本票据尚未报销；此便笺不是财务查重台账。")],
    ),
    taxi(
        "T12",
        "MEDIUM",
        OPS,
        "2026-04-12",
        "160.00",
        "HUMAN",
        "发票缺实际路线；计划行程只说明原定安排，不能证明实际乘车起讫地点。",
        missing_route=True,
        notes=[
            (
                "预计行程安排",
                "原定由服务中心前往客户园区；该安排形成于出行前，尚无平台实际行程回执。",
            )
        ],
    ),
]


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dates(item: dict) -> tuple[date, date | None, date]:
    occurred = date.fromisoformat(item.get("start") or item["day"])
    checkout = occurred + timedelta(days=item["nights"]) if item["kind"] == "HOTEL" else None
    submitted = (
        date.fromisoformat(item["submitted"])
        if "submitted" in item
        else (checkout or occurred) + timedelta(days=1)
    )
    return occurred, checkout, submitted


def write_document(slug: str, suffix: str, document_type: str, title: str, lines: list[str]):
    document_id = f"DOC-C16-{slug}-{suffix}"
    source = TARGET / "attachments" / f"{document_id}.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "\n".join(
            [
                "<!-- PAGE 1 -->",
                f"# {title}",
                *lines,
                f"附件编号：{document_id}",
                "本凭证为项目合成数据，不代表真实交易。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    pdf = build_pdf(source)
    page_text = PdfReader(pdf).pages[0].extract_text().strip()
    metadata = {
        "document_id": document_id,
        "fixture_key": f"SYNTHETIC_C16_{slug}_{suffix}",
        "document_type": document_type,
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
    _, chunks = canonicalize_and_chunk(
        [ParsedBlock(page_idx=0, block_index=0, kind="text", text=page_text)],
        metadata,
        parse_artifact_sha256=hashlib.sha256(page_text.encode()).hexdigest(),
    )
    evidence = {
        "evidence_id": f"EVID-C16-{slug}-{suffix}",
        "source_type": "ATTACHMENT",
        "document_id": document_id,
        "version": "1",
        "page": 1,
        "excerpt": page_text,
    }
    return metadata, [chunk.model_dump(mode="json") for chunk in chunks], evidence


def documents(item: dict):
    slug = item["slug"]
    occurred, checkout, _ = dates(item)
    kind = item["kind"]
    lines = [f"票据号：SYN-C16-{slug}", f"实际承担金额：{item['amount']} 元"]
    values = [("invoice_number", f"SYN-C16-{slug}"), ("amount", item["amount"])]
    if kind == "HOTEL":
        lines += [f"城市：{item['city']}", f"客房数量：{item['rooms']} 间客房"]
        values += [("city", item["city"]), ("room_count", item["rooms"])]
        if not item.get("stay_confirmation"):
            lines += [f"实际住宿：{occurred} 入住，{checkout} 退房。"]
            values += [("check_in", str(occurred)), ("check_out", str(checkout))]
        else:
            lines += ["本发票未列示入住和退房日期，请与酒店实际结算水单核对。"]
        lines += [
            "消费项目：会议指定酒店房费。"
            if item.get("conference")
            else "消费项目：公务驻场房费，无个人消费。"
        ]
    else:
        lines += [f"票据日期：{occurred}", f"消费项目：{item['description']}"]
        values += [("occurred_on", str(occurred))]
        if kind == "MEAL":
            lines += ["消费地点：杭州滨江客户园区餐厅", "餐饮明细：工作餐，不含礼品、娱乐或储值。"]
            if not item.get("missing_count"):
                lines += [f"实际参加人数：{item['count']} 人"]
                values += [("attendee_count", item["count"])]
        elif not item.get("missing_route"):
            route = actual_route(item)
            lines += [f"路线：{route}"]
            values += [("route", f"路线：{route}")]
        if item.get("payment_detail"):
            lines += [item["payment_detail"]]
    doc_type = {"HOTEL": "HOTEL_INVOICE", "MEAL": "MEAL_INVOICE", "TAXI": "TAXI_INVOICE"}[kind]
    main, chunks, evidence = write_document(slug, "INVOICE", doc_type, "合成电子费用凭证", lines)
    metas, evidence_rows = [main], [evidence]
    # Gold is constructed from the authored transaction, not by calling the extractor.
    fields = [
        {
            "field": name,
            "status": "PRESENT",
            "value": value,
            "raw_value": str(value),
            "document_id": main["document_id"],
            "page": 1,
        }
        for name, value in values
    ]
    absent = []
    if item.get("stay_confirmation"):
        absent += ["check_in", "check_out"]
    if item.get("missing_count"):
        absent += ["attendee_count"]
    if item.get("missing_route"):
        absent += ["route"]
    fields += [
        {"field": name, "status": "MISSING", "document_id": main["document_id"]} for name in absent
    ]
    if item.get("stay_confirmation"):
        meta, extra_chunks, extra_evidence = write_document(
            slug,
            "STAY",
            "ITINERARY",
            "酒店实际住宿结算水单",
            [
                f"对应票据：SYN-C16-{slug}",
                f"城市：{item['city']}",
                f"酒店确认：{occurred} 入住，{checkout} 退房。",
                f"实际客房：{item['rooms']} 间客房",
                "本水单证明已完成的住宿，不是预订或行程计划。",
            ],
        )
        metas.append(meta)
        chunks += extra_chunks
        evidence_rows.append(extra_evidence)
        fields += [
            {
                "field": name,
                "status": "PRESENT",
                "value": value,
                "raw_value": value,
                "document_id": meta["document_id"],
                "page": 1,
            }
            for name, value in [("check_in", str(occurred)), ("check_out", str(checkout))]
        ]
    for index, (title, body) in enumerate(item.get("notes", []), start=1):
        meta, extra_chunks, extra_evidence = write_document(
            slug,
            f"NOTE{index}",
            "OTHER",
            title,
            [f"关联申请：REQ-C16-{slug}", f"关联票据：SYN-C16-{slug}", body],
        )
        metas.append(meta)
        chunks += extra_chunks
        evidence_rows.append(extra_evidence)
    return metas, chunks, evidence_rows, fields


def actual_route(item: dict) -> str:
    if item.get("commute"):
        return "员工住所至公司固定办公地点"
    if item.get("station"):
        return "员工住所至杭州东高铁站"
    return item["route"]


def application(item: dict) -> dict:
    occurred, checkout, submitted = dates(item)
    value = {
        "request_id": f"REQ-C16-{item['slug']}",
        "expense_type": {"HOTEL": "住宿", "MEAL": "餐饮", "TAXI": "交通"}[item["kind"]],
        "currency": "CNY",
        "amount": item.get("application_amount", item["amount"]),
        "occurred_on": str(occurred),
        "submitted_on": str(submitted),
        "description": item["description"],
    }
    if item["kind"] == "HOTEL":
        value.update(
            city=item["city"],
            check_in=str(occurred),
            check_out=str(checkout),
            room_count=item["rooms"],
        )
        if item.get("conference"):
            value["description"] = "客户技术会议主办方指定酒店住宿，详见会议通知与例外材料"
    elif item["kind"] == "MEAL":
        value.update(city=item["city"], attendee_count=item.get("application_count", item["count"]))
        if item["slug"] == "M11":
            value["description"] += "；客户联调活动总预算 6200 元已审批，本笔仅为工作餐"
    else:
        value.update(
            transport_purpose="CUSTOMER_VISIT",
            origin_type="CUSTOMER_SITE",
            destination_type="CUSTOMER_SITE",
        )
        value["description"] += "：" + (
            "加班后整理客户材料，申请人称为客户拜访"
            if item.get("commute")
            else "公务出差前往高铁站"
            if item.get("station")
            else actual_route(item)
        )
        if item.get("station"):
            value.update(
                transport_purpose="BUSINESS_TRAVEL",
                origin_type="RESIDENCE",
                destination_type="STATION",
            )
    return value


def facts(item: dict, app: dict):
    slug = item["slug"]
    snapshot = f"STRUCTURED-DATA-C16-{slug}-1.6.0"
    employee = f"EMP-C16-{slug}"
    records, evidence = [], []

    def add(suffix, query_type, key, value, effective, *, available=None, required=True):
        record = {
            "fixture_id": f"C16-{slug}-{suffix}",
            "query_type": query_type,
            "record_key": key,
            "snapshot_version": f"{effective}T08:00:00+08:00",
            "effective_at": effective,
            "value": value,
            "consumer": "AGENT"
            if query_type in {"city_tier", "employee_department"}
            else "RULE_VALIDATOR",
        }
        if available is not None:
            record["available_amount"] = available
        records.append(record)
        if required:
            evidence.append(
                {
                    "evidence_id": f"EVID-C16-{slug}-{suffix}",
                    "source_type": "STRUCTURED_RECORD",
                    "fixture_id": record["fixture_id"],
                    "query_type": query_type,
                    "record_key": key,
                    "value": value,
                    "structured_data_snapshot_id": snapshot,
                    "snapshot_version": record["snapshot_version"],
                    "effective_at": effective,
                }
            )

    add(
        "EMPLOYEE",
        "employee_department",
        employee,
        item["department"],
        "2026-01-01",
        required=False,
    )
    if item["kind"] == "HOTEL":
        city = item["city"]
        if city == "常州市":
            before = app["occurred_on"] < "2026-05-15"
            add("CITY-OLD", "city_tier", city, "B", "2026-01-01", required=before)
            add("CITY-NEW", "city_tier", city, "A", "2026-05-15", required=not before)
        elif item.get("future_city"):
            add("CITY-FUTURE", "city_tier", city, "A", "2026-06-01", required=False)
        else:
            tier = "B" if city in {"苏州市", "长沙市"} else "A"
            add("CITY", "city_tier", city, tier, "2026-01-01")
    financial = item["kind"] == "HOTEL" or item["department"] == SALES
    if financial:
        if not item.get("missing_budget"):
            available = app["amount"] if item.get("exact_budget") else "12000.00"
            add(
                "BUDGET",
                "budget_status",
                f"{item['department']}:{app['submitted_on'][:7]}",
                "AVAILABLE",
                app["submitted_on"],
                available=available,
            )
        if not item.get("missing_permission"):
            add(
                "PERMISSION",
                "approval_permission",
                f"{employee}:{app['expense_type']}",
                "AUTHORIZED",
                "2026-01-01",
            )
        else:
            add(
                "OTHER-PERMISSION",
                "approval_permission",
                f"EMP-C16-OTHER:{app['expense_type']}",
                "AUTHORIZED",
                "2026-01-01",
                required=False,
            )
        add(
            "DUPLICATE",
            "duplicate_invoice",
            f"SYN-C16-{slug}",
            bool(item.get("duplicate")),
            app["submitted_on"],
        )
    if item["department"] == SALES and not item.get("commute"):
        add(
            "VISIT",
            "customer_visit_record",
            app["request_id"],
            f"VISIT-{slug}:CONFIRMED",
            app["occurred_on"],
        )
    return {
        "snapshot_id": snapshot,
        "source_type": "PROJECT_AUTHORED_SYNTHETIC",
        "license_id": "PROJECT-INTERNAL-SYNTHETIC",
        "records": records,
    }, evidence


def policy_anchor(chunks: list[dict], key: str, prefix: str, catalog: dict) -> str:
    evidence_id = f"EVID-C16-{key}-{prefix.split()[0]}"
    if evidence_id not in catalog:
        document_id = POLICIES[key]
        # MinerU sometimes omits the separator after an article number.
        prefix = prefix.rstrip()
        chunk = next(
            row
            for row in chunks
            if row["document_id"] == document_id
            and any(line.startswith(prefix) for line in row["text"].splitlines())
        )
        # Keep the whole article even when parsing wrapped it across several lines.
        start = chunk["text"].index(prefix)
        tail = chunk["text"][start:]
        excerpt = tail.split("\n\n", 1)[0].strip()
        authoring_path = ROOT / "data/raw/policies" / f"{document_id}.md"
        authoring_excerpt = next(
            line
            for line in authoring_path.read_text(encoding="utf-8").splitlines()
            if line.startswith(prefix)
        )
        catalog[evidence_id] = {
            "evidence_id": evidence_id,
            "source_type": "POLICY_DOCUMENT",
            "document_id": document_id,
            "version": chunk["version"],
            "page": chunk["page_number"],
            "section": " > ".join(chunk["title_path"]),
            "excerpt": excerpt,
            "authoring_excerpt": authoring_excerpt,
            "catalog_snapshot_id": chunk["catalog_snapshot_id"],
        }
    return evidence_id


def labels(item: dict, app: dict, attachments: list[dict], structured: list[dict], chunks, catalog):
    policy_ids = []

    def anchor(key, prefix):
        evidence_id = policy_anchor(chunks, key, prefix, catalog)
        policy_ids.append(evidence_id)
        return evidence_id

    invoice_id = attachments[0]["evidence_id"]
    results = [rule("RULE-EXPENSE-TYPE-IN-SCOPE", "PASS")]
    flags = []
    amount = Decimal(app["amount"])
    department_policy = item["department"] in {OPS, MKT}
    consistency = "FAIL" if item.get("application_amount") else "PASS"
    if item["kind"] == "HOTEL":
        if department_policy:
            anchor("OPS", "适用关系：")
        limit_id = (
            anchor("OPS", "第十八条 ") if department_policy else anchor("HOTEL", "第三十一条 ")
        )
        city_id = anchor("HOTEL", "第二十一条 ")
        anchor("HOTEL", "第二十四条 ") if item["city"] == "常州市" or item.get(
            "future_city"
        ) else None
        anchor("OPS", "第二十条 ") if department_policy else anchor("HOTEL", "第二十六条 ")
        if item.get("stay_confirmation"):
            anchor("HOTEL", "第十九条 ")
        deadline_id = (
            anchor("OPS", "第三十五条 ") if department_policy else anchor("HOTEL", "第四十条 ")
        )
        city_evidence = next((row for row in structured if row["query_type"] == "city_tier"), None)
        tier = city_evidence["value"] if city_evidence else None
        per_night = ({"A": 680, "B": 520} if department_policy else {"A": 600, "B": 450}).get(tier)
        limit = per_night * item["nights"] * item["rooms"] if per_night else None
        limit_outcome = "INDETERMINATE" if limit is None else "PASS" if amount <= limit else "FAIL"
        deadline = (
            date.fromisoformat(app["submitted_on"]) - date.fromisoformat(app["check_out"])
        ).days
        deadline_outcome = "PASS" if deadline <= (45 if department_policy else 30) else "FAIL"
        results += [
            rule("RULE-LODGING-NIGHT-COUNT", "PASS", invoice_id),
            rule("RULE-LODGING-AMOUNT-LIMIT", limit_outcome, limit_id, city_id),
            rule("RULE-LODGING-DOCUMENT-CONSISTENCY", "PASS", invoice_id),
            rule("RULE-SUBMISSION-TIMELINESS", deadline_outcome, deadline_id),
        ]
        if item.get("conference"):
            anchor("OPS", "第二十四条 ") if department_policy else anchor("HOTEL", "第三十五条 ")
            if department_policy:
                anchor("OPS", "第二十六条 ")
            flags.append("LODGING_EXCEPTION_REQUIRES_REVIEW")
        if deadline_outcome == "FAIL":
            flags.append("OVERDUE_EXCEPTION_REQUIRED")
        if item.get("missing_budget"):
            anchor("OPS", "第三十九条 ") if department_policy else anchor("HOTEL", "第四十二条 ")
    elif item["kind"] == "MEAL":
        consistency = (
            "INDETERMINATE"
            if item.get("missing_count") or item.get("application_count")
            else "PASS"
        )
        results += [rule("RULE-MEAL-DOCUMENT-CONSISTENCY", consistency, invoice_id)]
        if department_policy:
            anchor("OPS", "适用关系：")
            limit_id = anchor("OPS", "第二十九条 ")
            results += [
                rule(
                    "RULE-OPS-MKT-MEAL-LIMIT",
                    "PASS" if amount <= 160 * app["attendee_count"] else "FAIL",
                    limit_id,
                )
            ]
            if consistency == "INDETERMINATE":
                anchor("OPS", "第三十条 ")
                anchor("OPS", "第三十四条 ")
            if item["slug"] == "M11":
                anchor("OPS", "第八条 ")
                anchor("OPS", "第九条 ")
        else:
            limits = []
            if app["occurred_on"] <= "2026-06-30":
                limits.append(("OLD", "RULE-MEAL-LIMIT-USING-OLD-POLICY", 120))
            if app["occurred_on"] >= "2026-06-01":
                limits.append(("NEW", "RULE-MEAL-LIMIT-USING-NEW-POLICY", 150))
            outcomes = []
            for key, rule_id, per_person in limits:
                anchor(key, "第一条 ")
                anchor(key, "第二条 ")
                evidence_id = anchor(key, "第二十五条 ")
                outcome = "PASS" if amount <= per_person * app["attendee_count"] else "FAIL"
                outcomes.append(outcome)
                results.append(rule(rule_id, outcome, evidence_id))
            conflict = len(set(outcomes)) > 1
            results += [
                rule("RULE-POLICY-VERSION-UNIQUENESS", "FAIL" if conflict else "PASS", *policy_ids),
                rule(
                    "RULE-MEAL-LIMIT-DETERMINATION",
                    "INDETERMINATE" if conflict else outcomes[0],
                    *policy_ids,
                ),
            ]
            if conflict:
                flags = [
                    "RULE_VERSION_CONFLICT",
                    "MATERIAL_OUTCOME_DIVERGENCE",
                    "MISSING_AUTHORITATIVE_PRECEDENCE",
                ]
    else:
        if item.get("missing_route"):
            consistency = "INDETERMINATE"
        rule_id = (
            "RULE-SALES-TICKET-CONSISTENCY"
            if item["department"] == SALES
            else "RULE-AMOUNT-DATE-CONSISTENCY"
        )
        results += [rule(rule_id, consistency, invoice_id)]
        if item["department"] == SALES:
            if item.get("commute"):
                policy_id = anchor("SALES", "第十八条 ")
                anchor("SALES", "第二十二条 ")
                results += [rule("RULE-SALES-PRIVATE-COMMUTE", "FAIL", policy_id, invoice_id)]
                flags = ["CONFIRMED_PRIVATE_COMMUTE"]
            else:
                limit_id = anchor("SALES", "第十二条 ")
                anchor("SALES", "第十六条 ")
                results += [
                    rule("RULE-SALES-TAXI-LIMIT", "PASS" if amount <= 260 else "FAIL", limit_id),
                    rule("RULE-CUSTOMER-VISIT-CONFIRMED", "PASS"),
                ]
                if item.get("payment_detail"):
                    anchor("SALES", "第十五条 ")
                if item.get("missing_budget") or item.get("missing_permission"):
                    anchor("SALES", "第十七条 ")
                if item.get("duplicate"):
                    anchor("SALES", "第二十九条 ")
        elif department_policy:
            anchor("OPS", "适用关系：")
            limit_id = anchor("OPS", "第十三条 ")
            receipt_id = anchor("OPS", "第十七条 ")
            deadline_id = anchor("OPS", "第三十五条 ")
            elapsed = (
                date.fromisoformat(app["submitted_on"]) - date.fromisoformat(app["occurred_on"])
            ).days
            results += [
                rule("RULE-OPS-MKT-TRANSPORT-LIMIT", "PASS" if amount <= 300 else "FAIL", limit_id),
                rule("RULE-TRANSPORT-RECEIPT", "PASS", receipt_id, invoice_id),
                rule("RULE-SUBMISSION-DEADLINE", "PASS" if elapsed <= 45 else "FAIL", deadline_id),
            ]
            if item.get("payment_detail"):
                anchor("OPS", "第十五条 ")
            if elapsed > 45:
                anchor("OPS", "第三十六条 ")
                flags = ["OVERDUE_EXCEPTION_REQUIRED"]
        else:
            policy_id = anchor("TAXI", "第二十二条 ")
            results += [rule("RULE-TRANSPORT-PURPOSE-ELIGIBILITY", "PASS", policy_id, invoice_id)]
    if item["kind"] == "HOTEL" or item["department"] == SALES:
        for suffix, rule_id, outcome in [
            ("DUPLICATE", "RULE-DUPLICATE-INVOICE", "FAIL" if item.get("duplicate") else "PASS"),
            (
                "BUDGET",
                "RULE-BUDGET-AVAILABLE",
                "INDETERMINATE" if item.get("missing_budget") else "PASS",
            ),
            (
                "PERMISSION",
                "RULE-APPLICANT-PERMISSION",
                "INDETERMINATE" if item.get("missing_permission") else "PASS",
            ),
        ]:
            ids = [
                row["evidence_id"]
                for row in structured
                if row["evidence_id"].endswith(f"-{suffix}")
            ]
            results.append(rule(rule_id, outcome, *ids))
    return list(dict.fromkeys(policy_ids)), results, flags


def build() -> list[dict]:
    pdfmetrics.registerFont(UnicodeCIDFont(FONT))
    TARGET.mkdir(parents=True, exist_ok=True)
    policy_chunks = rows(ROOT / "data/fixtures/p04_chunks.jsonl")
    approvals, trajectories, retrieval, manifests, chunks, facts_rows = [], [], [], [], [], []
    evidence_catalog, index = {}, []
    for item in CASES:
        app = application(item)
        metas, attachment_chunks, attachment_evidence, fields = documents(item)
        fixture, structured_evidence = facts(item, app)
        policy_ids, results, flags = labels(
            item, app, attachment_evidence, structured_evidence, policy_chunks, evidence_catalog
        )
        fact_ids = [row["evidence_id"] for row in structured_evidence]
        attachment_ids = [row["evidence_id"] for row in attachment_evidence]
        evidence_catalog.update(
            (row["evidence_id"], row) for row in attachment_evidence + structured_evidence
        )
        identity = {
            "dataset_version": VERSION,
            "split": "validation",
            "source_type": "SYNTHETIC",
            "synthesis_method": "POLICY_GROUNDED_SYNTHETIC",
            "data_classification": "SYNTHETIC",
            "license_id": "PROJECT-INTERNAL-SYNTHETIC",
            "license_scope": "项目自编制度、交易与凭证，仅用于本项目开发、演示和评测；不含真实个人数据。",
            "reference_source_ids": [],
            "policy_catalog_snapshot_id": POLICY_SNAPSHOT,
            "structured_data_snapshot_id": fixture["snapshot_id"],
            "case_id": f"CASE-C16-{item['slug']}",
            "clause_family_id": f"CHALLENGE-{item['kind']}",
            "leakage_group_id": f"LG-C16-{item['kind']}",
            "difficulty": item["difficulty"],
        }
        recommendation = {
            "PASS": "PASS_RECOMMENDED",
            "REJECT": "REJECT_RECOMMENDED",
            "HUMAN": "HUMAN_REVIEW",
        }[item["outcome"]]
        stop = (
            ["ALL_NECESSARY_EVIDENCE_COVERED"]
            if item["outcome"] != "HUMAN"
            else [
                "REQUEST_DOCUMENTS",
                "EVIDENCE_ESCALATION",
                "UNRESOLVED_POLICY_VERSION_CONFLICT",
            ]
        )
        approvals.append(
            dict(
                **identity,
                sample_id=f"APP-C16-{item['slug']}",
                independence_class="NEW_INVOICE",
                input={
                    "applicant": {
                        "employee_id": f"EMP-C16-{item['slug']}",
                        "department_id": item["department"],
                        "display_name": "合成员工",
                    },
                    "application": app,
                    "documents": [
                        {
                            key: meta[key]
                            for key in (
                                "document_id",
                                "fixture_key",
                                "document_type",
                                "media_type",
                                "synthetic",
                            )
                        }
                        for meta in metas
                    ],
                },
                expected_extracted_fields=fields,
                expected_evidence_ids=policy_ids + attachment_ids + fact_ids,
                expected_rule_results=results,
                expected_risk_flags=flags,
                expected_recommendation=recommendation,
                acceptable_stop_reasons=stop,
                annotation={
                    "rationale": item["reason"],
                    "label_source": "AUTHORED_POLICY_AND_TRANSACTION",
                    "label_status": "DRAFT_REVIEWABLE",
                    "independently_reviewed": False,
                },
            )
        )
        questions = [
            {
                "intent": "APPLICABLE_POLICY_AND_PRECEDENCE",
                "text": "核对发生日及部门适用的标准、边界与例外。",
                "required_evidence_ids": policy_ids,
            },
            {
                "intent": "ACTUAL_TRANSACTION_FACTS",
                "text": "核对实际凭证与申请事实，区分计划、部门说明和原始凭证。",
                "required_evidence_ids": attachment_ids,
            },
        ]
        capabilities = [
            {
                "selected_tool_name": "policy_search",
                "purpose": "FIND_APPLICABLE_POLICY",
                "query_type": "",
            }
        ]
        city_ids = [
            row["evidence_id"] for row in structured_evidence if row["query_type"] == "city_tier"
        ]
        if city_ids:
            questions.append(
                {
                    "intent": "CITY_TIER_AS_OF_EXPENSE",
                    "text": "按费用发生日核对财务城市等级。",
                    "required_evidence_ids": city_ids,
                }
            )
            capabilities.append(
                {
                    "selected_tool_name": "structured_lookup",
                    "purpose": "LOOKUP_CITY_TIER",
                    "query_type": "city_tier",
                }
            )
        trajectories.append(
            dict(
                **identity,
                sample_id=f"TRAJ-C16-{item['slug']}",
                order_independent=True,
                allowed_tools=["policy_search", "structured_lookup"],
                hard_limits={
                    "max_agent_steps": 10,
                    "max_retrieval_rounds": 3,
                    "max_query_rewrites": 2,
                },
                required_sub_questions=questions,
                required_tool_capabilities=capabilities,
                required_evidence_ids=policy_ids + attachment_ids + fact_ids,
                acceptable_stop_reasons=stop,
                forbidden_actions=["FABRICATE_EVIDENCE", "ISSUE_FINAL_APPROVAL"],
            )
        )
        retrieval.append(
            dict(
                **identity,
                sample_id=f"RET-C16-{item['slug']}",
                query_family_id=f"QF-C16-{item['slug']}",
                query=f"{app['expense_type']} {app['description']} 应适用哪些制度和边界？",
                filters={
                    "expense_type": app["expense_type"],
                    "effective_at": app["occurred_on"],
                    "department_id": item["department"],
                },
                no_answer=False,
                relevant_evidence_groups=[
                    {"group_id": f"RG-C16-{item['slug']}-{i}", "any_of": [value], "relevance": 3}
                    for i, value in enumerate(policy_ids, start=1)
                ],
            )
        )
        manifests += metas
        chunks += attachment_chunks
        facts_rows.append(fixture)
        index.append(
            {
                "slug": item["slug"],
                "sample_id": f"APP-C16-{item['slug']}",
                "difficulty": item["difficulty"],
                "expense_type": app["expense_type"],
                "department_id": item["department"],
                "expected_recommendation": recommendation,
                "rationale": item["reason"],
                "documents": [meta["pdf_path"] for meta in metas],
                "policy_evidence_ids": policy_ids,
            }
        )
    for name, records in [
        ("approvals", approvals),
        ("agent_trajectories", trajectories),
        ("retrieval", retrieval),
        ("attachment_chunks", chunks),
    ]:
        write_jsonl(TARGET / f"{name}.jsonl", records)
    dump(
        TARGET / "attachment_manifest.json", {"dataset_version": VERSION, "attachments": manifests}
    )
    dump(
        TARGET / "attachment_evidence.json",
        {"dataset_version": VERSION, "items": list(evidence_catalog.values())},
    )
    dump(
        TARGET / "structured_snapshots.json", {"dataset_version": VERSION, "snapshots": facts_rows}
    )
    dump(TARGET / "case_index.json", {"dataset_version": VERSION, "cases": index})
    summary = {
        "dataset_version": VERSION,
        "status": "DRAFT_NOT_LIVE_EVALUATED",
        "samples": len(approvals),
        "difficulty": dict(Counter(row["difficulty"] for row in approvals)),
        "expense_types": dict(
            Counter(row["input"]["application"]["expense_type"] for row in approvals)
        ),
        "recommendations": dict(Counter(row["expected_recommendation"] for row in approvals)),
        "invoices": len(CASES),
        "pdf_documents": len(manifests),
        "structured_records": sum(len(snapshot["records"]) for snapshot in facts_rows),
        "synthetic": True,
        "external_blind_test": False,
        "frozen_test_modified": False,
    }
    dump(TARGET / "summary.json", summary)
    write_index(index)
    combine()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return facts_rows


def write_index(index: list[dict]) -> None:
    lines = [
        "# 40 条中高难合成案例",
        "",
        "均为 validation 草案，标签尚未经独立人员复核。难度是业务设计分级，不是模型实测难度。",
        "",
        "| 编号 | 难度 | 费用 | 预期建议 | 制度依据与判定理由 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in index:
        outcome = {
            "PASS_RECOMMENDED": "通过",
            "REJECT_RECOMMENDED": "驳回",
            "HUMAN_REVIEW": "人工",
        }[row["expected_recommendation"]]
        lines.append(
            f"| {row['slug']} | {row['difficulty']} | {row['expense_type']} | {outcome} | {row['rationale']} |"
        )
    lines += [
        "",
        "每条申请、PDF 路径与逐字制度锚点见 [case_index.json](case_index.json)；规则与抽取标签见 [approvals.jsonl](approvals.jsonl)。",
        "",
    ]
    (TARGET / "case-index.md").write_text("\n".join(lines), encoding="utf-8")


def combine() -> None:
    COMBINED.mkdir(parents=True, exist_ok=True)
    for name in ("approvals", "agent_trajectories", "retrieval"):
        # Old labels, scopes, IDs and budgets are kept exactly as authored; only bundle version changes.
        old = [
            dict(
                deepcopy(row),
                source_dataset_version=row["dataset_version"],
                dataset_version=VERSION,
            )
            for row in rows(BASE / f"{name}.jsonl")
        ]
        write_jsonl(COMBINED / f"{name}.jsonl", old + rows(TARGET / f"{name}.jsonl"))
    write_jsonl(
        COMBINED / "attachment_chunks.jsonl",
        rows(BASE / "attachment_chunks.jsonl") + rows(TARGET / "attachment_chunks.jsonl"),
    )
    for name, field in [("attachment_manifest", "attachments"), ("attachment_evidence", "items")]:
        old = json.loads((BASE / f"{name}.json").read_text(encoding="utf-8"))[field]
        new = json.loads((TARGET / f"{name}.json").read_text(encoding="utf-8"))[field]
        dump(COMBINED / f"{name}.json", dict(dataset_version=VERSION, **{field: old + new}))
    dump(
        COMBINED / "summary.json",
        {
            "dataset_version": VERSION,
            "status": "DRAFT_NOT_LIVE_EVALUATED",
            "samples": 100,
            "carried_over_samples": 60,
            "new_challenge_samples": 40,
            "source_datasets": ["draft_1_5_2", "challenge_1_6"],
            "frozen_test_modified": False,
            "external_blind_test": False,
        },
    )


def seed(snapshots: list[dict]) -> None:
    """Only isolated evaluation snapshots are inserted; no approval or production snapshot writes."""
    engine = create_engine(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    try:
        with engine.begin() as connection:
            for snapshot in snapshots:
                for record in snapshot["records"]:
                    connection.execute(
                        text("""
                        INSERT INTO structured_records (
                            snapshot_id, fixture_id, query_type, record_key, snapshot_version,
                            effective_at, value, available_amount, consumer
                        ) VALUES (
                            :snapshot_id, :fixture_id, :query_type, :record_key, :snapshot_version,
                            :effective_at, CAST(:value AS jsonb), :available_amount, :consumer
                        ) ON CONFLICT (snapshot_id, fixture_id) DO NOTHING
                    """),
                        dict(
                            record,
                            snapshot_id=snapshot["snapshot_id"],
                            value=json.dumps(record["value"], ensure_ascii=False),
                            available_amount=record.get("available_amount"),
                        ),
                    )
        print(
            f"Seeded {len(snapshots)} isolated C16 evaluation snapshots; no approval rows changed."
        )
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        action="store_true",
        help="also insert isolated evaluation facts in local PostgreSQL",
    )
    args = parser.parse_args()
    snapshots = build()
    if args.seed:
        seed(snapshots)


if __name__ == "__main__":
    main()
