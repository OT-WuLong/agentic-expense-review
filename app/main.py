"""FastAPI surface for the approval and rule workbench."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pymilvus import MilvusClient

from app.api_models import (
    ApprovalAccepted,
    ApprovalCreateRequest,
    CandidateApprovalRequest,
    DemoIdentity,
    HumanReviewRequest,
    LoginRequest,
    SessionInfo,
)
from app.approval_service import (
    INVOICE_DOCUMENT_TYPES,
    ApprovalService,
    checkpoint_state,
    jsonable,
    resume_review_background,
    run_approval_background,
    run_invoice_approval_background,
    saved_invoice_for_retry,
)
from app.auth import authenticate_token, end_session, identity_from_request, start_session
from app.database import IdempotencyConflictError, PostgresStore
from app.graph.approval import validate_human_submission
from app.graph.state import validate_state
from app.graph.workflow import projection_consistency
from app.models import ApprovalInput, ApprovalStatus, Document, ExpenseType, HumanReviewSubmission
from app.retrieval.hybrid import COLLECTION_NAME

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)
UPLOAD_ROOT = ROOT / "tmp/uploads"
WEB_ROOT = ROOT / "frontend/dist"
TERMINAL_STATUSES = {
    ApprovalStatus.COMPLETED.value,
    ApprovalStatus.BUSINESS_REJECTED.value,
    ApprovalStatus.INSUFFICIENT_EVIDENCE.value,
    ApprovalStatus.SYSTEM_ERROR.value,
}
RECOVERY_GRACE = timedelta(minutes=1)

app = FastAPI(
    title="Agentic RAG 费用预审 API",
    version="0.1.0",
    description="单公司 MVP：结构化申请、可审计 Agent 轨迹与人工复核。",
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next: Any) -> Any:
    supplied = request.headers.get("X-Request-ID", "")
    request_id = supplied if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", supplied) else str(uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def _error(request: Request, code: str, message: str, details: object = None) -> dict[str, object]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
            "details": details,
        }
    }


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_error(
            request,
            "VALIDATION_ERROR",
            "请求参数不合法",
            [
                {key: item[key] for key in ("loc", "type", "msg") if key in item}
                for item in exc.errors()
            ],
        ),
    )


@app.exception_handler(IdempotencyConflictError)
async def idempotency_error(request: Request, exc: IdempotencyConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=_error(request, "IDEMPOTENCY_CONFLICT", str(exc)),
    )


@app.exception_handler(LookupError)
async def lookup_error(request: Request, exc: LookupError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content=_error(request, "NOT_FOUND", str(exc)),
    )


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    if (
        exc.status_code in {401, 403, 404}
        and request.url.path.startswith("/api/v1/")
        and not (request.method == "GET" and request.url.path == "/api/v1/session")
    ):
        def record_denial() -> None:
            store = PostgresStore(connect_timeout=1)
            try:
                store.record_event(
                    request_id=None,
                    event_type="SECURITY_ACCESS_DENIED",
                    actor=getattr(request.state, "actor_id", "ANONYMOUS"),
                    payload={
                        "method": request.method,
                        "route": getattr(request.scope.get("route"), "path", "unknown"),
                        "status": exc.status_code,
                        "trace_id": getattr(request.state, "request_id", ""),
                    },
                    dedupe_key=f"security-denied:{uuid4()}",
                )
            finally:
                store.close()

        try:
            await asyncio.to_thread(record_denial)
        except Exception:  # noqa: BLE001 - never turn a denial into a successful request
            LOGGER.warning("security denial audit unavailable")
    message = exc.detail if isinstance(exc.detail, str) else "请求失败"
    details = exc.detail if not isinstance(exc.detail, str) else None
    return JSONResponse(
        status_code=exc.status_code,
        content=_error(request, "HTTP_ERROR", message, details),
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def unexpected_error(request: Request, _: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=_error(request, "INTERNAL_ERROR", "服务暂时不可用，请稍后重试"),
    )


def get_store() -> Iterator[PostgresStore]:
    store = PostgresStore()
    try:
        yield store
    finally:
        store.close()


Store = Annotated[PostgresStore, Depends(get_store)]


def current_identity(request: Request) -> DemoIdentity:
    identity = identity_from_request(request)
    request.state.actor_id = identity.actor_id
    return identity


Identity = Annotated[DemoIdentity, Depends(current_identity)]


def reviewer(identity: Identity) -> DemoIdentity:
    if identity.role != "FINANCE_REVIEWER":
        raise HTTPException(status_code=403, detail="该操作仅限财务复核员")
    return identity


Reviewer = Annotated[DemoIdentity, Depends(reviewer)]


def rule_admin(identity: Identity) -> DemoIdentity:
    if identity.role != "RULE_ADMIN":
        raise HTTPException(status_code=403, detail="该操作仅限规则管理员")
    return identity


RuleAdmin = Annotated[DemoIdentity, Depends(rule_admin)]


def rule_reader(identity: Identity) -> DemoIdentity:
    if identity.role not in {"RULE_ADMIN", "SYSTEM_ADMIN"}:
        raise HTTPException(status_code=403, detail="无权访问规则目录")
    return identity


RuleReader = Annotated[DemoIdentity, Depends(rule_reader)]


def _approval_scope(identity: DemoIdentity) -> tuple[list[str] | None, str | None]:
    if identity.role == "RULE_ADMIN":
        raise HTTPException(status_code=403, detail="规则管理员无权读取审批申请")
    if identity.role == "SYSTEM_ADMIN":
        return None, None
    return identity.department_ids, identity.actor_id if identity.role == "APPLICANT" else None


def _authorized_record(
    store: PostgresStore, request_id: str, identity: DemoIdentity
) -> dict[str, object]:
    departments, employee_id = _approval_scope(identity)
    record = store.approval_record(
        request_id, department_ids=departments, employee_id=employee_id
    )
    if record is None:
        raise HTTPException(status_code=404, detail="审批申请不存在或无权访问")
    return record


def _stale_running(record: dict[str, object]) -> bool:
    updated_at = record.get("updated_at")
    return isinstance(updated_at, datetime) and updated_at < datetime.now(UTC) - RECOVERY_GRACE


def _retry_mode(
    record: dict[str, object], checkpoint: dict[str, object] | None,
    identity: DemoIdentity, store: PostgresStore,
) -> str | None:
    current = str(record["status"])
    orphaned = current == ApprovalStatus.RUNNING.value and _stale_running(record)
    if (
        checkpoint
        and checkpoint["next_nodes"]
        and current in {
            ApprovalStatus.HUMAN_PENDING.value,
            ApprovalStatus.INSUFFICIENT_EVIDENCE.value,
            ApprovalStatus.SYSTEM_ERROR.value,
        }
        and identity.role in {"FINANCE_REVIEWER", "SYSTEM_ADMIN"}
        and store.accepted_human_review(str(record["request_id"])) is not None
    ):
        return "HUMAN_REVIEW"
    if current not in {
        ApprovalStatus.SYSTEM_ERROR.value,
        ApprovalStatus.HUMAN_PENDING.value,
    } and not orphaned:
        return None
    if checkpoint:
        if (
            checkpoint["next_nodes"]
            and not checkpoint["interrupted"]
            and identity.role in {"FINANCE_REVIEWER", "SYSTEM_ADMIN"}
        ):
            return "CHECKPOINT"
        return "UNRECOVERABLE" if orphaned and not checkpoint["next_nodes"] else None
    if current == ApprovalStatus.HUMAN_PENDING.value:
        return None
    if saved_invoice_for_retry(record):
        return "INVOICE_PARSE"
    if ApprovalService(store).unstarted_request_for_retry(record):
        return "INITIALIZE"
    return "UNRECOVERABLE" if orphaned else None


def _authorize_new_approval(identity: DemoIdentity, approval_input: ApprovalInput) -> None:
    if identity.role == "RULE_ADMIN":
        raise HTTPException(status_code=403, detail="规则管理员无权创建审批")
    applicant = approval_input.applicant
    if identity.role == "APPLICANT" and (
        applicant.employee_id != identity.actor_id
        or applicant.department_id not in identity.department_ids
    ):
        raise HTTPException(status_code=403, detail="申请人只能创建本人本部门申请")
    if identity.role == "FINANCE_REVIEWER" and applicant.department_id not in identity.department_ids:
        raise HTTPException(status_code=403, detail="无权创建其他部门申请")


async def _read_approval_file(file: UploadFile) -> tuple[bytes, str, str]:
    content = await file.read()
    if not content or len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="附件必须为 1 字节至 10MB")
    media_type = file.content_type or "application/octet-stream"
    signatures = {
        "application/pdf": (b"%PDF-", ".pdf"),
        "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
        "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
    }
    if media_type not in signatures or not content.startswith(signatures[media_type][0]):
        raise HTTPException(status_code=415, detail="仅支持 PDF、PNG 或 JPEG")
    return content, media_type, signatures[media_type][1]


def _save_approval_file(
    store: PostgresStore,
    *,
    request_id: str,
    document_id: str,
    document_type: str,
    media_type: str,
    suffix: str,
    content: bytes,
    actor: str,
    ingestion_status: str,
) -> tuple[Path, str]:
    folder = UPLOAD_ROOT / "documents" / request_id
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    target = folder / f"{document_id}-{digest}{suffix}"
    created = False
    try:
        with target.open("xb") as output:
            created = True
            output.write(content)
    except FileExistsError:
        if target.read_bytes() != content:
            raise IdempotencyConflictError("existing upload file does not match its content hash") from None
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    try:
        store.save_document(
            request_id=request_id,
            document_id=document_id,
            document_type=document_type,
            media_type=media_type,
            storage_uri=str(target.relative_to(ROOT)).replace("\\", "/"),
            sha256=digest,
        )
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    store.record_event(
        request_id=request_id,
        event_type="DOCUMENT_UPLOADED",
        actor=actor,
        payload={
            "document_id": document_id,
            "document_type": document_type,
            "sha256": digest,
            "ingestion_status": ingestion_status,
        },
        dedupe_key=f"document:{request_id}:{document_id}:{digest}",
    )
    return target, digest


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    checks = {"database": "unavailable", "milvus": "unavailable"}
    store = None
    try:
        store = PostgresStore()
        if store.schema_ready():
            checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness must report dependency failures
        LOGGER.warning("Database readiness failed: %s", type(exc).__name__)
    finally:
        if store is not None:
            store.close()

    milvus = None
    try:
        milvus = MilvusClient(uri=os.getenv("MILVUS_URI", "http://localhost:19530"), timeout=2)
        if COLLECTION_NAME in milvus.list_collections(timeout=2):
            checks["milvus"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness must report dependency failures
        LOGGER.warning("Milvus readiness failed: %s", type(exc).__name__)
    finally:
        if milvus is not None:
            milvus.close()

    healthy = all(result == "ok" for result in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unavailable", "checks": checks},
    )


@app.post("/api/v1/session/login", response_model=SessionInfo)
def login(payload: LoginRequest, response: Response) -> SessionInfo:
    identity = authenticate_token(payload.token)
    csrf = start_session(response, payload.token)
    return SessionInfo(identity=identity, csrf_token=csrf)


@app.get("/api/v1/session", response_model=SessionInfo)
def session(identity: Identity, request: Request) -> SessionInfo:
    return SessionInfo(identity=identity, csrf_token=getattr(request.state, "csrf_token", None))


@app.post("/api/v1/session/logout")
def logout(_: Identity, response: Response) -> dict[str, bool]:
    end_session(response)
    return {"logged_out": True}


@app.get("/api/v1/fixtures")
def fixtures(identity: Identity, store: Store) -> dict[str, object]:
    if identity.role not in {"FINANCE_REVIEWER", "SYSTEM_ADMIN"}:
        raise HTTPException(status_code=403, detail="演示案例仅限审核人员")
    return {"items": ApprovalService(store).fixture_summaries()}


@app.post(
    "/api/v1/approvals",
    response_model=ApprovalAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_approval(
    payload: ApprovalCreateRequest,
    background: BackgroundTasks,
    request: Request,
    identity: Identity,
    store: Store,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> ApprovalAccepted:
    if identity.role == "APPLICANT" and payload.fixture_case_id:
        raise HTTPException(status_code=403, detail="申请人不能启动演示案例")
    service = ApprovalService(store)
    approval_input, state, context, extracted_fields = service.prepare(
        payload.request,
        payload.fixture_case_id,
        payload.request_id,
        trace_id=request.state.request_id,
    )
    _authorize_new_approval(identity, approval_input)
    inserted = service.create_business_record(
        approval_input, extracted_fields, idempotency_key=idempotency_key
    )
    record = store.approval_record(approval_input.request_id)
    response_status = ApprovalStatus(record["status"] if record else ApprovalStatus.RUNNING)
    if inserted:
        background.add_task(run_approval_background, state, context)
    return ApprovalAccepted(
        request_id=approval_input.request_id,
        status=response_status,
        detail_url=f"/api/v1/approvals/{approval_input.request_id}",
        events_url=f"/api/v1/approvals/{approval_input.request_id}/events",
    )


@app.post(
    "/api/v1/approvals/with-invoice",
    response_model=ApprovalAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_approval_with_invoice(
    request_json: Annotated[str, Form(alias="request")],
    invoice: Annotated[UploadFile, File()],
    background: BackgroundTasks,
    request: Request,
    identity: Identity,
    store: Store,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> ApprovalAccepted:
    try:
        approval_input = ApprovalInput.model_validate_json(request_json)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="申请字段不合法") from exc
    if approval_input.documents:
        raise HTTPException(status_code=422, detail="发票由上传文件提供，不接受自报附件")
    _authorize_new_approval(identity, approval_input)
    content, media_type, suffix = await _read_approval_file(invoice)
    document_id = f"DOC-INVOICE-{hashlib.sha256(approval_input.request_id.encode()).hexdigest()[:24]}"
    document_type = INVOICE_DOCUMENT_TYPES[approval_input.application.expense_type]
    document = Document(
        document_id=document_id,
        document_type=document_type,
        media_type=media_type,
    )
    approval_input = approval_input.model_copy(update={"documents": [document]})
    inserted = ApprovalService(store).create_business_record(
        approval_input, [], idempotency_key=idempotency_key
    )
    if inserted:
        try:
            path, _ = _save_approval_file(
                store,
                request_id=approval_input.request_id,
                document_id=document_id,
                document_type=document_type,
                media_type=media_type,
                suffix=suffix,
                content=content,
                actor=identity.actor_id,
                ingestion_status="PARSING",
            )
        except Exception as exc:  # never leave a created request silently running
            store.mark_workflow_failed(
                approval_input.request_id,
                error=type(exc).__name__,
                message="发票保存失败",
                failure_key="invoice-save",
                trace_id=request.state.request_id,
            )
            raise HTTPException(status_code=500, detail="发票保存失败") from exc
        background.add_task(
            run_invoice_approval_background,
            approval_input,
            path,
            request.state.request_id,
        )
    else:
        existing = store.approval_detail(approval_input.request_id)
        saved = (
            next(
                (item for item in existing["documents"] if item["document_id"] == document_id),
                None,
            )
            if existing
            else None
        )
        if saved is None or saved["sha256"] != hashlib.sha256(content).hexdigest():
            raise IdempotencyConflictError("request ID was reused with a different invoice")
    record = store.approval_record(approval_input.request_id)
    response_status = ApprovalStatus(record["status"] if record else ApprovalStatus.RUNNING)
    return ApprovalAccepted(
        request_id=approval_input.request_id,
        status=response_status,
        detail_url=f"/api/v1/approvals/{approval_input.request_id}",
        events_url=f"/api/v1/approvals/{approval_input.request_id}/events",
    )


@app.get("/api/v1/approvals")
def list_approvals(
    identity: Identity,
    store: Store,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    approval_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
    human_pending: bool = False,
) -> dict[str, object]:
    departments, employee_id = _approval_scope(identity)
    items, total = store.list_approvals(
        page=page,
        page_size=page_size,
        status=approval_status,
        human_only=human_pending,
        department_ids=departments,
        employee_id=employee_id,
    )
    return {
        "items": jsonable(items),
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": math.ceil(total / page_size),
    }


@app.get("/api/v1/approvals/{request_id}")
def approval_detail(request_id: str, identity: Identity, store: Store) -> dict[str, object]:
    _authorized_record(store, request_id, identity)
    record = store.approval_detail(request_id)
    if record is None:
        raise LookupError(f"unknown approval request: {request_id}")
    checkpoint = checkpoint_state(request_id)
    return {
        "approval": jsonable(record),
        "workflow": checkpoint,
        "consistency": projection_consistency(record, checkpoint),
        "retry_mode": _retry_mode(record, checkpoint, identity, store),
    }


@app.get("/api/v1/approvals/{request_id}/evidence")
def approval_evidence(
    request_id: str,
    identity: Identity,
    store: Store,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    _authorized_record(store, request_id, identity)
    checkpoint = checkpoint_state(request_id)
    state = checkpoint.get("state", {}) if checkpoint else {}
    evidence = state.get("evidence", []) if isinstance(state, dict) else []
    start = (page - 1) * page_size
    return {
        "items": evidence[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": len(evidence),
        "pages": math.ceil(len(evidence) / page_size),
    }


@app.post("/api/v1/approvals/{request_id}/documents", status_code=201)
async def upload_document(
    request_id: str,
    identity: Identity,
    store: Store,
    document_id: Annotated[
        str, Form(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ],
    document_type: Annotated[str, Form(min_length=1)],
    file: Annotated[UploadFile, File()],
) -> dict[str, object]:
    _authorized_record(store, request_id, identity)
    if identity.role not in {"APPLICANT", "FINANCE_REVIEWER", "SYSTEM_ADMIN"}:
        raise HTTPException(status_code=403, detail="无权上传申请材料")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", request_id):
        raise HTTPException(status_code=422, detail="申请编号格式不合法")
    content, media_type, suffix = await _read_approval_file(file)
    _, digest = _save_approval_file(
        store,
        request_id=request_id,
        document_id=document_id,
        document_type=document_type,
        media_type=media_type,
        actor=identity.actor_id,
        suffix=suffix,
        content=content,
        ingestion_status="STORED_PENDING_EXTRACTION",
    )
    return {
        "document_id": document_id,
        "media_type": media_type,
        "sha256": digest,
        "ingestion_status": "STORED_PENDING_EXTRACTION",
    }


@app.post("/api/v1/approvals/{request_id}/review", status_code=202)
def submit_review(
    request_id: str,
    payload: HumanReviewRequest,
    background: BackgroundTasks,
    response: Response,
    identity: Reviewer,
    store: Store,
) -> dict[str, str]:
    _authorized_record(store, request_id, identity)
    checkpoint = checkpoint_state(request_id)
    if checkpoint is None:
        raise HTTPException(status_code=409, detail="审批工作流尚未建立")
    submission = HumanReviewSubmission(
        action=payload.action,
        operator_id=identity.actor_id,
        reviewer_role="FINANCE_REVIEWER",
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
        recommendation=payload.recommendation,
    )
    state = (
        validate_state(checkpoint["state"])  # type: ignore[arg-type]
        if checkpoint["interrupted"]
        else None
    )
    if checkpoint["interrupted"]:
        try:
            assert state is not None
            validate_human_submission(state, submission)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        _, current_status = store.claim_human_review(
            request_id,
            submission,
            expected_status=state["status"].value if state is not None else None,
        )
    except ValueError as exc:
        if isinstance(exc, IdempotencyConflictError):
            raise
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not checkpoint["interrupted"] and current_status in TERMINAL_STATUSES:
        response.status_code = 200
        return {"request_id": request_id, "status": current_status}
    if not checkpoint["interrupted"] and not checkpoint["next_nodes"]:
        raise HTTPException(status_code=409, detail="当前审批不在人工复核节点")
    if state is None:
        state = validate_state(checkpoint["state"])  # type: ignore[arg-type]
    context = ApprovalService(store).context_for_state(state)
    background.add_task(
        resume_review_background,
        request_id,
        context,
        submission,
    )
    return {"request_id": request_id, "status": "RESUMING"}


@app.post("/api/v1/approvals/{request_id}/retry", status_code=202)
def retry_approval(
    request_id: str,
    background: BackgroundTasks,
    request: Request,
    identity: Identity,
    store: Store,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> dict[str, str]:
    _authorized_record(store, request_id, identity)
    with store.request_lock(request_id) as acquired:
        if not acquired:
            raise HTTPException(status_code=409, detail="工作流仍在执行，请稍后刷新")
        record = store.approval_detail(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="审批申请不存在")
        current = str(record["status"])
        if current == ApprovalStatus.RUNNING.value and not _stale_running(record):
            return {"request_id": request_id, "status": current}
        checkpoint = checkpoint_state(request_id)
        mode = _retry_mode(record, checkpoint, identity, store)
        if mode is None:
            raise HTTPException(status_code=409, detail="没有可安全恢复的工作流或发票")
        if mode == "UNRECOVERABLE":
            store.mark_workflow_failed(
                request_id,
                error="UnrecoverableWorkflow",
                message="执行已中断，原始材料或 Checkpoint 不足以安全恢复",
                failure_key="unrecoverable-source",
                trace_id=request.state.request_id,
            )
            return {"request_id": request_id, "status": ApprovalStatus.SYSTEM_ERROR.value}
        if mode == "HUMAN_REVIEW":
            if checkpoint is None:
                raise HTTPException(status_code=409, detail="Checkpoint 已变化，请刷新后重试")
            submission = store.accepted_human_review(request_id)
            if submission is None:
                raise HTTPException(status_code=409, detail="已受理的人工复核不存在")
            state = validate_state(checkpoint["state"])  # type: ignore[arg-type]
            context = ApprovalService(store).context_for_state(state)
            store.record_event(
                request_id=request_id,
                event_type="HUMAN_REVIEW_RETRY_REQUESTED",
                actor=identity.actor_id,
                payload={"review_key": submission.idempotency_key, "retry_key": idempotency_key},
                dedupe_key=f"human-review-retry:{request_id}:{idempotency_key}",
            )
            background.add_task(resume_review_background, request_id, context, submission)
            return {"request_id": request_id, "status": "RESUMING"}
        if mode == "INVOICE_PARSE":
            source = saved_invoice_for_retry(record)
            if source is None:
                raise HTTPException(status_code=409, detail="已保存的发票不可用")
        elif mode == "CHECKPOINT":
            if checkpoint is None:
                raise HTTPException(status_code=409, detail="Checkpoint 已变化，请刷新后重试")
            state = validate_state(checkpoint["state"])  # type: ignore[arg-type]
            context = ApprovalService(store).context_for_state(state)
        else:
            service = ApprovalService(store)
            original = service.unstarted_request_for_retry(record)
            if original is None:
                raise HTTPException(status_code=409, detail="原始申请不可恢复")
            approval_input, fixture_case_id = original
            _, state, context, _ = service.prepare(
                approval_input,
                fixture_case_id,
                request_id if fixture_case_id else None,
                trace_id=request.state.request_id,
            )
        if not store.mark_workflow_retrying(
            request_id,
            actor=identity.actor_id,
            idempotency_key=idempotency_key,
            expected_status=current,
            stale_before=datetime.now(UTC) - RECOVERY_GRACE
            if current == ApprovalStatus.RUNNING.value else None,
        ):
            raise HTTPException(status_code=409, detail="工作流状态已变化，请刷新后重试")
        if mode == "INVOICE_PARSE":
            approval_input, path = source
            background.add_task(
                run_invoice_approval_background,
                approval_input, path, request.state.request_id,
            )
        else:
            background.add_task(
                run_approval_background, state, context,
                renew_deadline=mode == "CHECKPOINT",
            )
    return {"request_id": request_id, "status": ApprovalStatus.RUNNING.value}


@app.get("/api/v1/approvals/{request_id}/audit")
def approval_audit(request_id: str, identity: Identity, store: Store) -> dict[str, object]:
    _authorized_record(store, request_id, identity)
    items = store.audit_events_after(request_id, limit=1000)
    return {"items": jsonable(items), "total": len(items)}


@app.get("/api/v1/approvals/{request_id}/events")
async def approval_events(
    request_id: str,
    request: Request,
    identity: Identity,
    store: Store,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    _authorized_record(store, request_id, identity)
    try:
        cursor = max(after, int(last_event_id or 0))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID 必须是整数") from exc

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        idle_after_terminal = 0
        while not await request.is_disconnected():
            expires_at = getattr(request.state, "session_expires_at", None)
            if expires_at is not None and time.time() >= expires_at:
                break
            items = await asyncio.to_thread(
                store.audit_events_after, request_id, after_event_id=cursor, limit=200
            )
            if items:
                idle_after_terminal = 0
                for item in items:
                    cursor = int(item["event_id"])
                    data = json.dumps(jsonable(item), ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\nevent: {item['event_type']}\ndata: {data}\n\n"
            else:
                record = await asyncio.to_thread(store.approval_record, request_id)
                if record and str(record["status"]) in TERMINAL_STATUSES:
                    idle_after_terminal += 1
                    if idle_after_terminal >= 2:
                        break
                yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/policies")
def list_policies(
    identity: Identity,
    store: Store,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    policies = ApprovalService(store).policies()
    if identity.role in {"APPLICANT", "FINANCE_REVIEWER"}:
        policies = [
            item
            for item in policies
            if "*" in (item.get("department_ids") or [])
            or set(item.get("department_ids") or []) & set(identity.department_ids)
        ]
    start = (page - 1) * page_size
    return {
        "items": jsonable(policies[start : start + page_size]),
        "page": page,
        "page_size": page_size,
        "total": len(policies),
        "pages": math.ceil(len(policies) / page_size),
    }


@app.post("/api/v1/policies", status_code=201)
async def upload_policy(
    identity: RuleAdmin,
    store: Store,
    document_id: Annotated[
        str, Form(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ],
    title: Annotated[str, Form(min_length=1)],
    expense_type: Annotated[ExpenseType, Form()],
    version: Annotated[str, Form(min_length=1)],
    effective_from: Annotated[date, Form()],
    file: Annotated[UploadFile, File()],
) -> dict[str, object]:
    content = await file.read()
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=415, detail="制度文件必须是 PDF")
    if not content or len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="制度文件必须为 1 字节至 20MB")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="制度文件内容不是 PDF")
    digest = hashlib.sha256(content).hexdigest()
    folder = UPLOAD_ROOT / "policies"
    target = folder / f"{document_id}.pdf"
    payload = {
        "document_id": document_id,
        "title": title,
        "expense_type": expense_type.value,
        "version": version,
        "effective_from": effective_from.isoformat(),
        "status": "UPLOADED_PENDING_INDEX",
        "sha256": digest,
        "storage_uri": str(target.relative_to(ROOT)).replace("\\", "/"),
    }
    existing = next(
        (item for item in ApprovalService(store).policies() if item["document_id"] == document_id),
        None,
    )
    if existing is not None:
        if (
            existing.get("source") == "UPLOAD"
            and all(existing.get(key) == value for key, value in payload.items())
            and target.is_file()
            and target.read_bytes() == content
        ):
            return payload
        raise IdempotencyConflictError("policy document ID is already in use")
    folder.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with target.open("xb") as output:
            created = True
            if output.write(content) != len(content):
                raise OSError("policy upload was written incompletely")
    except FileExistsError:
        if target.read_bytes() != content:
            raise IdempotencyConflictError("policy document ID is already in use") from None
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    store.record_event(
        request_id=None,
        event_type="POLICY_UPLOADED",
        actor=identity.actor_id,
        payload=payload,
        dedupe_key=f"policy-upload:{document_id}:{digest}",
    )
    return payload


@app.get("/api/v1/rules")
def list_rules(_: RuleReader, store: Store) -> dict[str, object]:
    return jsonable(store.rule_catalog())


@app.post("/api/v1/rules/candidates/{candidate_id}/approve")
def approve_candidate(
    candidate_id: int,
    payload: CandidateApprovalRequest,
    identity: RuleAdmin,
    store: Store,
) -> dict[str, object]:
    try:
        store.approve_candidate(
            candidate_id,
            reviewer_id=identity.actor_id,
            source_document_id=payload.source_document_id,
            effective_from=payload.effective_from,
            parameters=payload.parameters,
            replay_cases=[item.model_dump(exclude_none=True) for item in payload.replay_cases],
        )
    except IdempotencyConflictError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"candidate_id": candidate_id, "status": "APPROVED", "published": False}


@app.post("/api/v1/rules/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: int, identity: RuleAdmin, store: Store) -> dict[str, object]:
    store.reject_candidate(candidate_id, reviewer_id=identity.actor_id)
    return {"candidate_id": candidate_id, "status": "REJECTED"}


@app.post("/api/v1/rules/candidates/{candidate_id}/publish")
def publish_candidate(candidate_id: int, identity: RuleAdmin, store: Store) -> dict[str, object]:
    rule_id = store.publish_rule(candidate_id, reviewer_id=identity.actor_id)
    return {"candidate_id": candidate_id, "rule_id": rule_id, "published": True}


@app.post("/api/v1/rules/candidates/{candidate_id}/rollback")
def rollback_candidate(candidate_id: int, identity: RuleAdmin, store: Store) -> dict[str, object]:
    store.rollback_rule(candidate_id, reviewer_id=identity.actor_id)
    return {"candidate_id": candidate_id, "status": "SUPERSEDED"}


@app.post("/api/v1/rules/{rule_id}/publish", include_in_schema=True)
def publish_rule(rule_id: str, identity: RuleAdmin, store: Store) -> dict[str, object]:
    """Compatibility path: the identifier is an approved candidate ID, never a formal rule ID."""
    try:
        candidate_id = int(rule_id)
    except ValueError as exc:
        raise LookupError(f"unknown candidate: {rule_id}") from exc
    return publish_candidate(candidate_id, identity, store)


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str) -> FileResponse:
    """Serve built Vue files and history routes without turning unknown APIs into HTML."""

    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API 路由不存在")
    dist = WEB_ROOT.resolve()
    asset = (dist / path).resolve()
    if asset.is_relative_to(dist) and asset.is_file():
        return FileResponse(asset)
    if Path(path).suffix or not (dist / "index.html").is_file():
        raise HTTPException(status_code=404, detail="页面不存在")
    return FileResponse(dist / "index.html")
