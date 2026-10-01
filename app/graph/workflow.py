"""Small P09 facade for durable starts, interrupts, and resumptions."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import Enum
from threading import Lock
from typing import Any, get_args, get_type_hints

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command
from psycopg import Connection
from psycopg.rows import dict_row
from pydantic import BaseModel
from sqlalchemy.engine import make_url

from app.database import DEFAULT_DATABASE_URL
from app.graph.approval import ApprovalRuntime, build_approval_graph
from app.graph.state import ApprovalState, validate_state
from app.models import HumanReviewSubmission

_CHECKPOINT_MODULES = {"app.models", "app.agents.contracts"}
_CHECKPOINT_SETUP_LOCK = Lock()
_CHECKPOINT_SETUP_URLS: set[str] = set()


def _checkpoint_allowed_types() -> list[tuple[str, str]]:
    found: set[type[object]] = set()
    visited: set[object] = set()

    def visit(annotation: object) -> None:
        if annotation in visited:
            return
        visited.add(annotation)
        if isinstance(annotation, type) and annotation.__module__ in _CHECKPOINT_MODULES:
            found.add(annotation)
            if issubclass(annotation, BaseModel):
                for field in annotation.model_fields.values():
                    visit(field.annotation)
        for argument in get_args(annotation):
            visit(argument)

    for annotation in get_type_hints(ApprovalState, include_extras=True).values():
        visit(annotation)
    return sorted((item.__module__, item.__name__) for item in found)


# HumanReviewPayload and HumanReviewSubmission intentionally stay outside this
# allowlist: interrupt/resume transports their JSON dumps, never Python objects.
_CHECKPOINT_ALLOWED_TYPES = _checkpoint_allowed_types()


def approval_thread_id(request_id: str) -> str:
    return f"approval:{request_id}"


def approval_config(request_id: str) -> dict[str, object]:
    return {
        "configurable": {"thread_id": approval_thread_id(request_id)},
        # Three bounded retrieval rounds plus guardrail, rule and human nodes exceed 20.
        "recursion_limit": 40,
    }


def _checkpoint_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("checkpoint storage requires a PostgreSQL URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _setup_checkpointer_once(url: str, checkpointer: PostgresSaver) -> None:
    if url in _CHECKPOINT_SETUP_URLS:
        return
    with _CHECKPOINT_SETUP_LOCK:
        if url not in _CHECKPOINT_SETUP_URLS:
            checkpointer.setup()
            _CHECKPOINT_SETUP_URLS.add(url)


@contextmanager
def open_persistent_approval_graph(
    database_url: str | None = None,
) -> Iterator[Any]:
    url = _checkpoint_url(
        database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    serializer = JsonPlusSerializer(
        allowed_msgpack_modules=_CHECKPOINT_ALLOWED_TYPES
    )
    with Connection.connect(
        url,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    ) as connection:
        checkpointer = PostgresSaver(connection, serde=serializer)
        _setup_checkpointer_once(url, checkpointer)
        yield build_approval_graph(checkpointer)


def _snapshot_result(snapshot: Any) -> dict[str, object]:
    result = dict(snapshot.values)
    interrupts = [item for task in snapshot.tasks for item in task.interrupts]
    if interrupts:
        result["__interrupt__"] = interrupts
    return result


def _plain(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def projection_consistency(
    record: Mapping[str, object] | None,
    checkpoint: Mapping[str, object] | None,
) -> dict[str, object]:
    state = checkpoint.get("state", {}) if checkpoint else {}
    if not isinstance(state, Mapping):
        state = {}
    fields = ("status", "recommendation", "risk_level", "final_decision")
    mismatched = (
        [
            field
            for field in fields
            if _plain(state.get(field)) != _plain(record.get(field) if record else None)
        ]
        if state
        else []
    )
    status = (
        "MISSING_CHECKPOINT"
        if not state
        else "MISSING_BUSINESS_PROJECTION"
        if record is None
        else "DIVERGED"
        if mismatched
        else "CONSISTENT"
    )
    result: dict[str, object] = {
        "status": status,
        "mismatched_fields": mismatched,
    }
    if state:
        result.update(
            execution_authority="LANGGRAPH_CHECKPOINT",
            business_authority="APPROVAL_REQUESTS_AND_AUDIT_EVENTS",
        )
    return result


def workflow_consistency(
    graph: Any, request_id: str, runtime: ApprovalRuntime
) -> dict[str, object]:
    snapshot = graph.get_state(approval_config(request_id))
    record = runtime.database.approval_record(request_id)

    checkpoint_summary = {
        field: _plain(snapshot.values.get(field)) if snapshot.values else None
        for field in ("status", "recommendation", "risk_level", "final_decision")
    }
    projection_summary = {
        field: _plain(record.get(field)) if record else None
        for field in ("status", "recommendation", "risk_level", "final_decision")
    }
    comparison = projection_consistency(
        record,
        {"state": snapshot.values} if snapshot.values else None,
    )
    return {
        "consistency": comparison["status"],
        "checkpoint": checkpoint_summary,
        "business_projection": projection_summary,
        "mismatched_fields": comparison["mismatched_fields"],
        "next_nodes": list(snapshot.next),
        "execution_authority": comparison.get("execution_authority", "LANGGRAPH_CHECKPOINT"),
        "business_authority": comparison.get(
            "business_authority", "APPROVAL_REQUESTS_AND_AUDIT_EVENTS"
        ),
    }


def start_approval(
    graph: Any,
    state: Mapping[str, object],
    runtime: ApprovalRuntime,
    *,
    idempotency_key: str,
) -> dict[str, object]:
    validated = validate_state(state)
    request_id = validated["request_id"]
    runtime.database.create_approval(
        request_id=request_id,
        thread_id=approval_thread_id(request_id),
        idempotency_key=idempotency_key,
        applicant=validated["applicant"],
        application=validated["application"],
        documents=validated.get("documents", []),
    )
    config = approval_config(request_id)
    snapshot = graph.get_state(config)
    if snapshot.values:
        if any(task.interrupts for task in snapshot.tasks) or not snapshot.next:
            return _snapshot_result(snapshot)
        return graph.invoke(None, config=config, context=runtime)
    return graph.invoke(validated, config=config, context=runtime)


def resume_human_review(
    graph: Any,
    request_id: str,
    runtime: ApprovalRuntime,
    submission: HumanReviewSubmission | Mapping[str, object],
    *,
    operator_id: str,
    reviewer_role: str,
) -> dict[str, object]:
    submitted = (
        submission.model_dump(mode="python")
        if isinstance(submission, HumanReviewSubmission)
        else dict(submission)
    )
    review = HumanReviewSubmission.model_validate(
        {
            **submitted,
            "operator_id": operator_id,
            "reviewer_role": reviewer_role,
        }
    )
    config = approval_config(request_id)
    snapshot = graph.get_state(config)
    if not snapshot.values:
        raise LookupError(f"unknown approval thread: {request_id}")
    if any(task.interrupts for task in snapshot.tasks):
        return graph.invoke(
            Command(resume=review.model_dump(mode="json")),
            config=config,
            context=runtime,
        )
    if snapshot.next:
        return graph.invoke(None, config=config, context=runtime)
    return _snapshot_result(snapshot)
