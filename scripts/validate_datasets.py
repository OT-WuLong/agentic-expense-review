"""Validate P03 evaluation fixtures and their frozen test snapshot.

This script is deliberately read-only: it neither repairs labels nor rewrites a freeze.
"""

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.models import (
    Applicant,
    Application,
    Document,
    EvidenceItem,
    ExtractedField,
    Recommendation,
    RuleResult,
)

DATASETS = ("retrieval", "approvals", "agent_trajectories", "security")
SPLITS = {"dev", "validation", "test"}
SHA_FILES = {
    "source_manifest": "data/fixtures/source_manifest.json",
    "structured_records": "data/fixtures/structured_records.json",
    "reference_structured_records": "data/fixtures/structured_records_reference.json",
    "reference_evidence": "evals/datasets/reference_evidence.json",
    "golden_cases": "data/fixtures/golden_cases.json",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text))


def valid_date(value: object) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def valid_money(value: object, *, positive: bool = False) -> bool:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        return False
    try:
        money = Decimal(value)
    except InvalidOperation:
        return False
    return money.is_finite() and (money > 0 if positive else money >= 0)


def read_bundle(root: Path) -> tuple[dict[str, list[dict]], dict, dict, dict, dict, list[str]]:
    errors: list[str] = []
    datasets: dict[str, list[dict]] = {}
    for name in DATASETS:
        path = root / "evals/datasets" / f"{name}.jsonl"
        rows: list[dict] = []
        try:
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise TypeError("expected JSON object")
                    rows.append(row)
                except (json.JSONDecodeError, TypeError) as exc:
                    errors.append(f"{path.relative_to(root)}:{line_no}: invalid JSONL: {exc}")
        except OSError as exc:
            errors.append(f"{path.relative_to(root)}: cannot read: {exc}")
        datasets[name] = rows

    metadata: list[dict] = []
    for key in ("source_manifest", "structured_records", "reference_structured_records", "reference_evidence"):
        path = root / SHA_FILES[key]
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(item, dict):
                raise TypeError("expected JSON object")
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"{SHA_FILES[key]}: cannot read JSON object: {exc}")
            item = {}
        metadata.append(item)
    return datasets, *metadata, errors


def validate_bundle(
    root: Path, datasets: dict[str, list[dict]], manifest: dict, structured: dict,
    ref_structured: dict, catalog: dict, golden: dict | None = None
) -> list[str]:
    errors: list[str] = []
    if golden is None:
        try:
            golden = json.loads((root / SHA_FILES["golden_cases"]).read_text(encoding="utf-8"))
            if not isinstance(golden, dict):
                raise TypeError("expected JSON object")
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"{SHA_FILES['golden_cases']}: cannot read JSON object: {exc}")
            golden = {}
    docs = manifest.get("policies", []) + manifest.get("attachments", [])
    document_by_id = {item.get("document_id"): item for item in docs if isinstance(item, dict)}
    policy_ids = {item.get("document_id") for item in manifest.get("policies", [])}
    attachment_ids = {item.get("document_id") for item in manifest.get("attachments", [])}
    source_ids = {item.get("source_id") for item in manifest.get("reference_sources", [])}
    external_dependencies = manifest.get("declared_external_dependencies", [])
    external_ids = {
        item.get("document_id") for item in external_dependencies if isinstance(item, dict)
    }
    evidence = catalog.get("items", [])
    evidence_by_id = {item.get("evidence_id"): item for item in evidence if isinstance(item, dict)}
    records = ref_structured.get("records", [])
    record_by_id = {item.get("fixture_id"): item for item in records if isinstance(item, dict)}
    if len(record_by_id) != len(records):
        errors.append("reference_structured_records: duplicate or missing fixture_id")
    if ref_structured.get("snapshot_id") != manifest.get("reference_structured_data_snapshot_id"):
        errors.append("reference_structured_records: snapshot mismatch")
    if not ref_structured.get("license_id") or ref_structured.get("source_type") != "PROJECT_AUTHORED_SYNTHETIC":
        errors.append("reference_structured_records: missing licence/synthetic provenance")
    for record in records:
        ident = record.get("fixture_id", "<missing>")
        if not all(record.get(key) for key in ("query_type", "record_key", "snapshot_version", "consumer")):
            errors.append(f"{ident}: incomplete structured record provenance")
        if not valid_date(record.get("effective_at")) or "value" not in record:
            errors.append(f"{ident}: invalid structured effective date/value")
        if "available_amount" in record and not valid_money(record["available_amount"]):
            errors.append(f"{ident}: invalid structured available_amount")
        if "document_id" in record or "page" in record:
            errors.append(f"{ident}: structured record fabricates document/page")
    if len(document_by_id) != len(docs):
        errors.append("source_manifest: duplicate or missing document_id")
    if len(evidence_by_id) != len(evidence):
        errors.append("reference_evidence: duplicate or missing evidence_id")
    if len(source_ids) != len(manifest.get("reference_sources", [])):
        errors.append("source_manifest: duplicate or missing reference source_id")
    if len(external_ids) != len(external_dependencies) or external_ids & set(document_by_id):
        errors.append("source_manifest: invalid or conflicting external dependency ID")
    for item in external_dependencies:
        if (
            item.get("availability") != "DECLARED_NOT_INCLUDED"
            or item.get("evaluation_use") != "OUT_OF_SCOPE_REFERENCE_ONLY"
            or item.get("original_text_imported") is not False
        ):
            errors.append("source_manifest: invalid external dependency boundary")
    if catalog.get("visibility") != "EVALUATOR_ONLY":
        errors.append("reference_evidence: catalog must be EVALUATOR_ONLY")
    if catalog.get("policy_catalog_snapshot_id") != manifest.get(
        "reference_policy_catalog_snapshot_id"
    ):
        errors.append("reference_evidence: policy catalog snapshot mismatch")

    # The source manifest binds every authored PDF to its bytes, page count and licence.
    page_cache: dict[tuple[str, int], str] = {}
    for doc in docs:
        ident = doc.get("document_id", "<missing>")
        pdf_path = doc.get("pdf_path")
        if not doc.get("license_id") or not doc.get("source_type") or doc.get("synthetic") is not True:
            errors.append(f"{ident}: missing licence/provenance/synthetic marker")
        allowed = {"PROJECT_AUTHORED_SYNTHETIC", "REFERENCE_INFORMED_SYNTHETIC"}
        if doc.get("source_type") not in allowed:
            errors.append(f"{ident}: illegal manifest source_type")
        if doc.get("source_type") == "REFERENCE_INFORMED_SYNTHETIC" and not doc.get("reference_source_ids"):
            errors.append(f"{ident}: reference-informed source lacks real reference IDs")
        if doc.get("reference_source_ids") and not set(doc["reference_source_ids"]) <= source_ids:
            errors.append(f"{ident}: unknown real reference source ID")
        if not isinstance(pdf_path, str):
            errors.append(f"{ident}: missing PDF path")
            continue
        resolved = (root / pdf_path).resolve()
        if not resolved.is_relative_to(root.resolve()):
            errors.append(f"{ident}: PDF path escapes project root")
            continue
        try:
            if sha256(resolved) != doc.get("pdf_sha256"):
                errors.append(f"{ident}: PDF SHA-256 mismatch")
            reader = PdfReader(resolved)
            if reader.is_encrypted or len(reader.pages) != doc.get("page_count"):
                errors.append(f"{ident}: PDF page count/encryption mismatch")
            else:
                for page_no, page in enumerate(reader.pages, 1):
                    page_cache[(ident, page_no)] = compact(page.extract_text() or "")
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"{ident}: cannot inspect PDF: {exc}")
        if "effective_from" in doc:
            start, end = doc.get("effective_from"), doc.get("effective_to")
            if not valid_date(start) or (end is not None and (not valid_date(end) or end < start)):
                errors.append(f"{ident}: invalid policy effective dates")
            departments = doc.get("department_ids")
            if (
                not doc.get("expense_types")
                or not isinstance(departments, list)
                or not departments
                or any(not isinstance(item, str) or not item.strip() for item in departments)
            ):
                errors.append(f"{ident}: missing policy scope")

    errors.extend(validate_golden(golden, manifest, structured, document_by_id, page_cache))

    for item in evidence:
        ident = item.get("evidence_id", "<missing>")
        try:
            EvidenceItem.model_validate(item)
        except ValidationError as exc:
            errors.append(f"{ident}: invalid evidence source: {exc.errors()[0]['msg']}")
        if item.get("source_type") == "STRUCTURED_RECORD":
            record = record_by_id.get(item.get("fixture_id"))
            if not record:
                errors.append(f"{ident}: unknown structured fixture ID")
                continue
            if item.get("structured_data_snapshot_id") != ref_structured.get("snapshot_id"):
                errors.append(f"{ident}: structured snapshot mismatch")
            for key in ("query_type", "record_key", "snapshot_version", "effective_at", "value", "consumer"):
                if item.get(key) != record.get(key):
                    errors.append(f"{ident}: structured {key} mismatch")
            if item.get("available_amount") != record.get("available_amount"):
                errors.append(f"{ident}: structured available_amount mismatch")
            continue
        doc = document_by_id.get(item.get("document_id"))
        page = item.get("page")
        if not doc:
            errors.append(f"{ident}: unknown source document")
            continue
        expected_source = "POLICY_DOCUMENT" if doc["document_id"] in policy_ids else "ATTACHMENT"
        if doc["document_id"] not in policy_ids | attachment_ids or item.get("source_type") != expected_source:
            errors.append(f"{ident}: source type does not match manifest document kind")
        if item.get("source_type") not in {"POLICY_DOCUMENT", "ATTACHMENT"}:
            errors.append(f"{ident}: invalid source type")
        if item.get("version") != doc.get("version"):
            errors.append(f"{ident}: source version mismatch")
        if type(page) is not int or page < 1 or page > doc.get("page_count", 0):
            errors.append(f"{ident}: invalid source page")
        else:
            source_page = page_cache.get((doc["document_id"], page), "")
            if not compact(item.get("excerpt", "")) or compact(item["excerpt"]) not in source_page:
                errors.append(f"{ident}: excerpt absent from actual PDF page")
            if item.get("section") and compact(item["section"]) not in source_page:
                errors.append(f"{ident}: section absent from actual PDF page")
        if item.get("source_type") == "POLICY_DOCUMENT":
            for key in ("effective_from", "effective_to", "published_status", "authority_level", "priority"):
                if item.get(key) != doc.get(key):
                    errors.append(f"{ident}: policy {key} mismatch")
            if item.get("catalog_snapshot_id") != doc.get("catalog_snapshot_id"):
                errors.append(f"{ident}: policy snapshot mismatch")

    all_rows = [(name, row) for name in DATASETS for row in datasets.get(name, [])]
    id_seen: set[str] = set()
    split_by_clue: dict[tuple[str, str], str] = {}
    versions: set[str] = set()
    approval_by_case: dict[str, dict] = {}
    for name, row in all_rows:
        ident = row.get("sample_id")
        if not isinstance(ident, str) or not ident:
            errors.append(f"{name}: missing sample_id")
            continue
        if ident in id_seen:
            errors.append(f"{ident}: duplicate sample_id")
        id_seen.add(ident)
        split = row.get("split")
        if split not in SPLITS:
            errors.append(f"{ident}: illegal split {split!r}")
        version = row.get("dataset_version")
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            errors.append(f"{ident}: invalid dataset_version")
        else:
            versions.add(version)
        if row.get("source_type") != "SYNTHETIC" or row.get("synthesis_method") not in {
            "REFERENCE_INFORMED_SYNTHETIC", "PROJECT_AUTHORED_SYNTHETIC"
        }:
            errors.append(f"{ident}: invalid synthetic provenance")
        if not row.get("license_id") or not row.get("license_scope"):
            errors.append(f"{ident}: missing licence")
        references = row.get("reference_source_ids")
        if not isinstance(references, list) or not set(references) <= source_ids:
            errors.append(f"{ident}: invalid real-reference provenance")
        elif row.get("synthesis_method") == "REFERENCE_INFORMED_SYNTHETIC" and not references:
            errors.append(f"{ident}: reference-informed sample lacks references")
        if row.get("policy_catalog_snapshot_id") != manifest.get(
            "reference_policy_catalog_snapshot_id"
        ):
            errors.append(f"{ident}: policy snapshot mismatch")
        clues = {
            "leakage_group_id": row.get("leakage_group_id"),
            "clause_family_id": row.get("clause_family_id"),
            "query_family_id": row.get("query_family_id"),
            "case_id": row.get("case_id"),
            "pair_id": row.get("pair_id"),
        }
        if not row.get("leakage_group_id"):
            errors.append(f"{ident}: missing leakage_group_id")
        if name in {"retrieval", "approvals", "agent_trajectories"} and not row.get(
            "clause_family_id"
        ):
            errors.append(f"{ident}: missing clause_family_id")
        if name == "retrieval" and not row.get("query_family_id"):
            errors.append(f"{ident}: missing query_family_id")
        if name == "retrieval" and isinstance(row.get("query"), str):
            clues["normalized_query"] = normalized(row["query"])
        if name == "security" and isinstance(row.get("payload"), dict):
            query = row["payload"].get("query")
            if isinstance(query, str):
                clues["normalized_query"] = normalized(query)
        if name == "approvals":
            application = row.get("input", {}).get("application", {})
            clues["request_id"] = application.get("request_id")
            for attachment in row.get("input", {}).get("documents", []):
                doc = document_by_id.get(attachment.get("document_id"))
                if doc:
                    clues[f"attachment_sha:{doc['document_id']}"] = doc.get("pdf_sha256")
            case_id = row.get("case_id")
            if case_id in approval_by_case:
                errors.append(f"{ident}: duplicate approval case_id {case_id}")
            approval_by_case[case_id] = row
        for clue_type, value in clues.items():
            if not isinstance(value, str) or not value:
                continue
            key = (clue_type.split(":")[0], value)
            prior = split_by_clue.setdefault(key, split)
            if prior != split:
                errors.append(f"{ident}: cross-split {key[0]} {value!r}: {prior} vs {split}")

    if len(versions) != 1:
        errors.append(f"datasets: inconsistent versions {sorted(versions)}")

    for name, row in all_rows:
        ident = row.get("sample_id", "<missing>")
        if name == "retrieval":
            if not isinstance(row.get("query"), str) or not row["query"].strip():
                errors.append(f"{ident}: missing query")
            filters = row.get("filters", {})
            if not isinstance(filters, dict) or not valid_date(filters.get("effective_at")):
                errors.append(f"{ident}: illegal effective_at")
                filters = {}
            department_id = filters.get("department_id")
            if not isinstance(department_id, str) or not department_id.strip():
                errors.append(f"{ident}: missing retrieval department_id")
                department_id = None
            expense_type = filters.get("expense_type")
            if not isinstance(expense_type, str) or not expense_type.strip():
                errors.append(f"{ident}: missing retrieval expense_type")
                expense_type = None
            groups = row.get("relevant_evidence_groups")
            if not isinstance(row.get("no_answer"), bool) or not isinstance(groups, list) or row[
                "no_answer"
            ] != (len(groups) == 0):
                errors.append(f"{ident}: no_answer/groups inconsistency")
                groups = groups if isinstance(groups, list) else []
            seen_groups: set[str] = set()
            for group in groups:
                group_id = group.get("group_id")
                if not group_id or group_id in seen_groups:
                    errors.append(f"{ident}: duplicate/missing evidence group ID")
                seen_groups.add(group_id)
                if type(group.get("relevance")) is not int or not 1 <= group["relevance"] <= 3:
                    errors.append(f"{ident}: illegal relevance label")
                if not group.get("any_of") or len(set(group["any_of"])) != len(group["any_of"]):
                    errors.append(f"{ident}: empty/duplicate evidence alternatives")
                for eid in group.get("any_of", []):
                    check_evidence(ident, eid, evidence_by_id, document_by_id, record_by_id, row, errors,
                                   department_id=department_id,
                                   expense_type=expense_type,
                                   effective_at=filters.get("effective_at"))
        elif name == "approvals":
            validate_approval(ident, row, document_by_id, record_by_id, evidence_by_id,
                              page_cache, errors)
        elif name == "agent_trajectories":
            approval = approval_by_case.get(row.get("case_id"), {})
            approval_input = approval.get("input", {})
            applicant = approval_input.get("applicant", {})
            applicant = applicant if isinstance(applicant, dict) else {}
            application = approval_input.get("application", {})
            application = application if isinstance(application, dict) else {}
            invoice_numbers = {
                str(field.get("value"))
                for field in approval.get("expected_extracted_fields", [])
                if field.get("field") == "invoice_number" and field.get("value") is not None
            }
            dates = {"effective_at": application.get("occurred_on"),
                     "submitted_on": application.get("submitted_on")}
            for eid in row.get("required_evidence_ids", []):
                check_evidence(ident, eid, evidence_by_id, document_by_id, record_by_id, row, errors,
                               department_id=applicant.get("department_id"),
                               employee_id=applicant.get("employee_id"),
                               request_id=application.get("request_id"),
                               expense_type=application.get("expense_type"),
                               invoice_numbers=invoice_numbers,
                               **dates)
            for question in row.get("required_sub_questions", []):
                for eid in question.get("required_evidence_ids", []):
                    check_evidence(ident, eid, evidence_by_id, document_by_id, record_by_id, row, errors,
                                   department_id=applicant.get("department_id"),
                                   employee_id=applicant.get("employee_id"),
                                   request_id=application.get("request_id"),
                                   expense_type=application.get("expense_type"),
                                   invoice_numbers=invoice_numbers,
                                   **dates)
            limits = row.get("hard_limits", {})
            if not isinstance(limits, dict) or any(
                type(limits.get(key)) is not int or limits[key] < 0
                for key in ("max_agent_steps", "max_retrieval_rounds", "max_query_rewrites")
            ):
                errors.append(f"{ident}: invalid hard limits")
            if not row.get("required_sub_questions") or not row.get("required_tool_capabilities"):
                errors.append(f"{ident}: missing trajectory requirements")
            if not row.get("allowed_tools") or not row.get("acceptable_stop_reasons"):
                errors.append(f"{ident}: missing tools/stop labels")
        elif name == "security":
            if row.get("variant") not in {"ATTACK", "BENIGN"} or not row.get("attack_family_id"):
                errors.append(f"{ident}: illegal security variant/family")
            if row.get("expected_block") is not (row.get("variant") == "ATTACK"):
                errors.append(f"{ident}: variant/expected_block inconsistency")
            if not row.get("pair_id") or not row.get("expected_reason_code"):
                errors.append(f"{ident}: missing pair/reason label")
            permission = row.get("permission_context", {})
            if not isinstance(permission, dict):
                errors.append(f"{ident}: invalid permission context")
                permission = {}
            request_department = permission.get("request_department_id")
            allowed_departments = permission.get("allowed_department_ids")
            if not isinstance(request_department, str) or not request_department.strip():
                errors.append(f"{ident}: missing permission request_department_id")
            if (
                not isinstance(allowed_departments, list)
                or not allowed_departments
                or any(not isinstance(item, str) or not item.strip() for item in allowed_departments)
                or len(set(allowed_departments)) != len(allowed_departments)
            ):
                errors.append(f"{ident}: invalid permission allowed_department_ids")
            elif request_department not in allowed_departments:
                errors.append(f"{ident}: permission department mismatch")
            if row.get("attack_family_id") == "AUTHORIZATION_SCOPE_OVERRIDE":
                payload = row.get("payload", {})
                payload_department = (
                    payload.get("filters", {}).get("department_id")
                    if isinstance(payload, dict) and isinstance(payload.get("filters"), dict)
                    else None
                )
                if row.get("variant") == "ATTACK" and (
                    not isinstance(payload_department, str)
                    or not isinstance(allowed_departments, list)
                    or payload_department in allowed_departments
                ):
                    errors.append(f"{ident}: authorization attack lacks unauthorized department")
                if (
                    row.get("variant") == "BENIGN"
                    and isinstance(payload_department, str)
                    and isinstance(allowed_departments, list)
                    and payload_department not in allowed_departments
                ):
                    errors.append(f"{ident}: benign query exceeds department scope")
            if not row.get("objective") or not row.get("payload"):
                errors.append(f"{ident}: missing security objective/payload")

    for name in ("retrieval", "agent_trajectories"):
        for row in datasets.get(name, []):
            case_id = row.get("case_id")
            if case_id is None:
                continue
            approval = approval_by_case.get(case_id)
            if not approval:
                errors.append(f"{row.get('sample_id')}: missing approval case {case_id}")
            elif (row.get("split"), row.get("leakage_group_id")) != (
                approval.get("split"), approval.get("leakage_group_id")
            ):
                errors.append(f"{row.get('sample_id')}: derived case split/group mismatch")

    pairs: dict[str, list[dict]] = defaultdict(list)
    families: dict[str, set[str]] = defaultdict(set)
    for row in datasets.get("security", []):
        pairs[row.get("pair_id")].append(row)
        families[row.get("attack_family_id")].add(row.get("variant"))
    for pair_id, rows in pairs.items():
        if len(rows) != 2 or {r.get("variant") for r in rows} != {"ATTACK", "BENIGN"}:
            errors.append(f"security pair {pair_id}: requires exactly ATTACK + BENIGN")
        elif any(
            (r.get("split"), r.get("leakage_group_id"), r.get("attack_family_id")) !=
            (rows[0].get("split"), rows[0].get("leakage_group_id"), rows[0].get("attack_family_id"))
            for r in rows
        ):
            errors.append(f"security pair {pair_id}: split/group/family mismatch")
    for family, variants in families.items():
        if variants != {"ATTACK", "BENIGN"}:
            errors.append(f"security family {family}: lacks attack or benign control")

    for target_split in ("validation", "test"):
        labels = {
            row.get("expected_recommendation") for row in datasets.get("approvals", [])
            if row.get("split") == target_split
        }
        if labels != set(Recommendation):
            errors.append(f"approvals {target_split}: missing class support {set(Recommendation) - labels}")
    return errors


def validate_golden(
    golden: dict, manifest: dict, structured: dict, documents: dict[str, dict],
    page_cache: dict[tuple[str, int], str],
) -> list[str]:
    errors: list[str] = []
    expected_policy_snapshot = manifest.get("golden_policy_catalog_snapshot_id")
    expected_structured_snapshot = manifest.get("golden_structured_data_snapshot_id")

    def strings(value: object):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    snapshot_ids = {
        match
        for value in strings(golden)
        for match in re.findall(
            r"POLICY-CATALOG-SYN-[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*", value
        )
    }
    for snapshot_id in sorted(snapshot_ids - {expected_policy_snapshot}):
        errors.append(f"golden_cases: old policy snapshot ID {snapshot_id}")

    records = structured.get("records", [])
    record_by_id = {
        record.get("fixture_id"): record for record in records if isinstance(record, dict)
    }
    for case in golden.get("cases", []):
        case_id = case.get("case_id", "<missing>")
        context = case.get("fixture_context", {})
        if context.get("policy_catalog_snapshot_id") != expected_policy_snapshot:
            errors.append(f"{case_id}: golden policy snapshot mismatch")
        if (
            context.get("structured_data_snapshot_id") != expected_structured_snapshot
            or structured.get("snapshot_id") != expected_structured_snapshot
        ):
            errors.append(f"{case_id}: golden structured snapshot mismatch")
        for evidence in case.get("critical_evidence", []):
            evidence_id = evidence.get("evidence_id", "<missing>")
            ident = f"{case_id}/{evidence_id}"
            if evidence.get("source_type") == "STRUCTURED_RECORD":
                record = record_by_id.get(evidence.get("fixture_id"))
                if not record:
                    errors.append(f"{ident}: unknown golden structured fixture ID")
                    continue
                if evidence.get("structured_data_snapshot_id") != expected_structured_snapshot:
                    errors.append(f"{ident}: golden structured snapshot mismatch")
                for key in (
                    "query_type", "record_key", "snapshot_version", "effective_at",
                    "value", "available_amount", "consumer",
                ):
                    if key in evidence and evidence.get(key) != record.get(key):
                        errors.append(f"{ident}: golden structured {key} mismatch")
                continue

            doc = documents.get(evidence.get("document_id"))
            if not doc:
                errors.append(f"{ident}: unknown golden source document")
                continue
            is_policy = "effective_from" in doc
            allowed_sources = (
                {"POLICY_DOCUMENT", "DOCUMENT_CONTEXT"} if is_policy else {"ATTACHMENT"}
            )
            if evidence.get("source_type") not in allowed_sources:
                errors.append(f"{ident}: golden source type does not match manifest document kind")
            if evidence.get("version") != doc.get("version"):
                errors.append(f"{ident}: golden source version mismatch")
            if is_policy:
                if evidence.get("catalog_snapshot_id") != expected_policy_snapshot:
                    errors.append(f"{ident}: golden policy snapshot mismatch")
                for key in (
                    "effective_from", "effective_to", "published_status", "authority_level",
                    "priority", "supersedes_document_id",
                ):
                    if key in evidence and evidence.get(key) != doc.get(key):
                        errors.append(f"{ident}: golden policy {key} mismatch")
            page = evidence.get("page")
            if type(page) is not int or page < 1 or page > doc.get("page_count", 0):
                errors.append(f"{ident}: invalid golden source page")
                continue
            source_page = page_cache.get((doc["document_id"], page), "")
            for key in ("section", "excerpt"):
                value = evidence.get(key)
                if not isinstance(value, str) or not compact(value):
                    errors.append(f"{ident}: missing golden {key}")
                elif compact(value) not in source_page:
                    errors.append(f"{ident}: golden {key} absent from actual PDF page")
    return errors


def check_evidence(
    ident: str, eid: str, catalog: dict, documents: dict, records: dict, row: dict,
    errors: list[str],
    *, department_id: str | None = None, employee_id: str | None = None,
    request_id: str | None = None,
    expense_type: str | None = None, invoice_numbers: set[str] | None = None,
    effective_at: str | None = None, submitted_on: str | None = None,
) -> None:
    item = catalog.get(eid)
    if not item:
        errors.append(f"{ident}: unknown evidence ID {eid}")
        return
    if item.get("source_type") == "STRUCTURED_RECORD":
        record = records.get(item.get("fixture_id"))
        if record:
            record_key = record.get("record_key")
            query_type = record.get("query_type")
            if (
                query_type == "budget_status"
                and department_id
                and not str(record_key).startswith(f"{department_id}:")
            ):
                errors.append(f"{ident}: structured budget outside department scope {eid}")
            if (
                query_type == "approval_permission"
                and employee_id
                and expense_type
                and record_key != f"{employee_id}:{expense_type}"
            ):
                errors.append(f"{ident}: structured permission outside applicant scope {eid}")
            if (
                query_type == "duplicate_invoice"
                and invoice_numbers is not None
                and record_key not in invoice_numbers
            ):
                errors.append(f"{ident}: structured duplicate check outside invoice scope {eid}")
            if query_type == "customer_visit_record" and request_id and record_key != request_id:
                errors.append(f"{ident}: structured customer visit outside request scope {eid}")
        bound = effective_at if item.get("query_type") == "city_tier" or submitted_on is None else submitted_on
        if record and valid_date(bound) and valid_date(record.get("effective_at")) and record["effective_at"] > bound:
            errors.append(f"{ident}: future-dated structured evidence ID {eid}")
        return
    doc = documents.get(item.get("document_id"))
    if not doc:
        return  # Catalog source error is already reported above.
    if item.get("source_type") == "POLICY_DOCUMENT":
        departments = doc.get("department_ids", [])
        departments = departments if isinstance(departments, list) else []
        if department_id and "*" not in departments and department_id not in departments:
            errors.append(f"{ident}: policy evidence outside department scope {eid}")
        if expense_type and expense_type not in doc.get("expense_types", []):
            errors.append(f"{ident}: policy evidence outside expense scope {eid}")
        if item.get("catalog_snapshot_id") != row.get("policy_catalog_snapshot_id"):
            errors.append(f"{ident}: wrong policy snapshot for evidence {eid}")
        if effective_at and valid_date(effective_at):
            start, end = doc.get("effective_from"), doc.get("effective_to")
            if not valid_date(start) or effective_at < start or (end and effective_at > end):
                errors.append(f"{ident}: evidence {eid} not effective at query date")


def validate_approval(
    ident: str, row: dict, documents: dict, records: dict, catalog: dict,
    page_cache: dict[tuple[str, int], str], errors: list[str]
) -> None:
    input_ = row.get("input", {})
    if not isinstance(input_, dict) or not isinstance(input_.get("application"), dict):
        errors.append(f"{ident}: missing approval input/application")
        return
    application = input_["application"]
    applicant = input_.get("applicant", {})
    department_id = applicant.get("department_id") if isinstance(applicant, dict) else None
    employee_id = applicant.get("employee_id") if isinstance(applicant, dict) else None
    if not isinstance(department_id, str) or not department_id.strip():
        errors.append(f"{ident}: missing applicant department_id")
        department_id = None
    top_eids = row.get("expected_evidence_ids")
    rules = row.get("expected_rule_results")
    if not isinstance(top_eids, list) or not top_eids:
        errors.append(f"{ident}: empty approval evidence gold")
        top_eids = []
    if not isinstance(rules, list) or not rules:
        errors.append(f"{ident}: empty approval rule gold")
        rules = []
    try:
        Application.model_validate({k: v for k, v in application.items() if k != "request_id"})
        Applicant.model_validate(applicant)
    except ValidationError as exc:
        errors.append(f"{ident}: invalid amount/date/application/applicant: {exc.errors()[0]['msg']}")
    if not application.get("request_id"):
        errors.append(f"{ident}: missing request_id")
    attached_ids: set[str] = set()
    for attachment in input_.get("documents", []):
        try:
            Document.model_validate(attachment)
        except ValidationError as exc:
            errors.append(f"{ident}: invalid attachment: {exc.errors()[0]['msg']}")
        doc = documents.get(attachment.get("document_id"))
        if not doc or any(
            attachment.get(key) != doc.get(key)
            for key in ("fixture_key", "document_type", "media_type", "synthetic")
        ):
            errors.append(f"{ident}: attachment missing or manifest mismatch")
        attached_ids.add(attachment.get("document_id"))
    extracted = row.get("expected_extracted_fields", [])
    extracted_names: set[str] = set()
    for field in extracted:
        try:
            ExtractedField.model_validate(field)
        except ValidationError as exc:
            errors.append(f"{ident}: invalid extracted field: {exc.errors()[0]['msg']}")
        if field.get("document_id") not in attached_ids:
            errors.append(f"{ident}: extracted field cites non-attached document")
        doc = documents.get(field.get("document_id"))
        if doc and (type(field.get("page")) is not int or field["page"] > doc.get("page_count", 0)):
            errors.append(f"{ident}: extracted field illegal page")
        if field.get("status") == "PRESENT" and doc and type(field.get("page")) is int:
            source_value = field.get("raw_value") if field.get("raw_value") is not None else field.get("value")
            if not compact(str(source_value)) or compact(str(source_value)) not in page_cache.get(
                (doc["document_id"], field["page"]), ""
            ):
                errors.append(f"{ident}: extracted field value absent from actual PDF page")
        if field.get("field") in {"amount"} and not valid_money(field.get("value"), positive=True):
            errors.append(f"{ident}: invalid extracted amount")
        if field.get("field") in {"occurred_on", "submitted_on"} and not valid_date(field.get("value")):
            errors.append(f"{ident}: invalid extracted date")
        extracted_names.add(field.get("field"))
    invoice_numbers = {
        str(field.get("value"))
        for field in extracted
        if field.get("field") == "invoice_number" and field.get("value") is not None
    }
    for eid in top_eids:
        check_evidence(ident, eid, catalog, documents, records, row, errors,
                       department_id=department_id,
                       employee_id=employee_id,
                       request_id=application.get("request_id"),
                       expense_type=application.get("expense_type"),
                       invoice_numbers=invoice_numbers,
                       effective_at=application.get("occurred_on"),
                       submitted_on=application.get("submitted_on"))
    if row.get("expected_recommendation") not in set(Recommendation):
        errors.append(f"{ident}: illegal approval recommendation")
    if not isinstance(row.get("acceptable_stop_reasons"), list) or not row[
        "acceptable_stop_reasons"
    ]:
        errors.append(f"{ident}: missing stop labels")
    seen_rules: set[str] = set()
    for rule in rules:
        try:
            RuleResult.model_validate(rule)
        except ValidationError as exc:
            errors.append(f"{ident}: invalid rule result: {exc.errors()[0]['msg']}")
        rule_id = rule.get("rule_id")
        if rule_id in seen_rules:
            errors.append(f"{ident}: duplicate expected rule ID")
        seen_rules.add(rule_id)
        for eid in rule.get("evidence_ids", []):
            if eid not in top_eids:
                errors.append(f"{ident}: rule evidence ID {eid} omitted from approval gold")
            check_evidence(ident, eid, catalog, documents, records, row, errors,
                           department_id=department_id,
                           employee_id=employee_id,
                           request_id=application.get("request_id"),
                           expense_type=application.get("expense_type"),
                           invoice_numbers=invoice_numbers,
                           effective_at=application.get("occurred_on"),
                           submitted_on=application.get("submitted_on"))
        for key in ("computed_limit", "actual_amount"):
            if key in rule and not valid_money(rule[key]):
                errors.append(f"{ident}: invalid rule {key}")
        for ref in rule.get("input_refs", []):
            if ref.startswith("input."):
                value: object = input_
                for part in ref.split(".")[1:]:
                    value = value.get(part) if isinstance(value, dict) else None
                if value is None:
                    errors.append(f"{ident}: missing rule input ref {ref}")
            elif ref.startswith("extracted_fields."):
                if ref.split(".")[-1] not in extracted_names:
                    errors.append(f"{ident}: missing extracted rule ref {ref}")
            else:
                errors.append(f"{ident}: illegal rule input ref {ref}")


def freeze_metadata(root: Path, datasets: dict[str, list[dict]], manifest: dict,
                    structured: dict, ref_structured: dict, catalog: dict) -> dict:
    versions = {row.get("dataset_version") for rows in datasets.values() for row in rows}
    result = {
        "schema_version": "dataset-test-freeze/v1",
        "frozen_on": datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
        "dataset_version": next(iter(versions)) if len(versions) == 1 else None,
        "policy_catalog_snapshot_id": manifest.get("reference_policy_catalog_snapshot_id"),
        "datasets": {},
        "sources": {},
    }
    for name in DATASETS:
        rows = datasets[name]
        test_rows = sorted((row for row in rows if row.get("split") == "test"),
                           key=lambda row: row["sample_id"])
        canonical = json.dumps(test_rows, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
        result["datasets"][name] = {
            "total_count": len(rows),
            "test_count": len(test_rows),
            "file_sha256": sha256(root / "evals/datasets" / f"{name}.jsonl"),
            "test_canonical_sha256": hashlib.sha256(canonical).hexdigest(),
        }
    for name, rel_path in SHA_FILES.items():
        doc = {"source_manifest": manifest, "structured_records": structured,
               "reference_structured_records": ref_structured,
               "reference_evidence": catalog}.get(name)
        if doc is None:
            doc = json.loads((root / rel_path).read_text(encoding="utf-8"))
        count_key = {"source_manifest": None, "structured_records": "records",
                     "reference_structured_records": "records",
                     "reference_evidence": "items", "golden_cases": "cases"}[name]
        count = (len(doc.get("policies", [])) + len(doc.get("attachments", []))) if name == "source_manifest" else len(doc.get(count_key, []))
        version_key = {"source_manifest": "manifest_version", "structured_records": "snapshot_id",
                       "reference_structured_records": "snapshot_id",
                       "reference_evidence": "catalog_version", "golden_cases": "dataset_version"}[name]
        result["sources"][name] = {"version": doc.get(version_key), "count": count,
                                    "sha256": sha256(root / rel_path)}
    return result


def validate_freeze(root: Path, current: dict) -> list[str]:
    path = root / "evals/datasets/test.freeze.json"
    if not path.exists():
        return ["test.freeze.json: missing frozen test snapshot; use --print-freeze to generate a candidate"]
    try:
        frozen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"test.freeze.json: cannot read: {exc}"]
    errors: list[str] = []
    if not valid_date(frozen.get("frozen_on")):
        errors.append("test.freeze.json: invalid frozen_on")
    for key in ("schema_version", "dataset_version", "policy_catalog_snapshot_id", "datasets", "sources"):
        if frozen.get(key) != current.get(key):
            errors.append(f"test.freeze.json: frozen {key} no longer matches current data")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-freeze", action="store_true", help="print canonical freeze metadata")
    args = parser.parse_args()
    datasets, manifest, structured, ref_structured, catalog, errors = read_bundle(ROOT)
    if not errors:
        errors.extend(validate_bundle(ROOT, datasets, manifest, structured, ref_structured, catalog))
    if not errors:
        current = freeze_metadata(ROOT, datasets, manifest, structured, ref_structured, catalog)
        if args.print_freeze:
            print(json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        errors.extend(validate_freeze(ROOT, current))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"Dataset validation failed: {len(errors)} error(s).", file=sys.stderr)
        return 1
    counts = ", ".join(f"{name}={len(rows)}" for name, rows in datasets.items())
    print(f"Dataset validation PASS: {counts}; PDF SHA/pages/evidence and freeze verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
