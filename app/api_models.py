"""HTTP contracts for the P10 approval workbench."""

from typing import Literal

from pydantic import Field, model_validator

from app.models import (
    ApprovalInput,
    ApprovalStatus,
    DateOnly,
    HumanReviewAction,
    Recommendation,
    StrictModel,
)


class DemoIdentity(StrictModel):
    actor_id: str
    display_name: str
    role: Literal["APPLICANT", "FINANCE_REVIEWER", "RULE_ADMIN", "SYSTEM_ADMIN"]
    department_ids: list[str] = Field(default_factory=list)


class LoginRequest(StrictModel):
    token: str = Field(min_length=24)


class SessionInfo(StrictModel):
    identity: DemoIdentity
    csrf_token: str | None = None


class ApprovalCreateRequest(StrictModel):
    """Create from a hand-entered request or clone one frozen demo fixture."""

    request: ApprovalInput | None = None
    fixture_case_id: str | None = None
    request_id: str | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> "ApprovalCreateRequest":
        if (self.request is None) == (self.fixture_case_id is None):
            raise ValueError("provide exactly one of request or fixture_case_id")
        if self.fixture_case_id and not self.request_id:
            raise ValueError("fixture creation requires a caller-generated request_id")
        if self.request is not None and self.request_id is not None:
            raise ValueError("request_id belongs inside request for hand-entered approvals")
        if self.request is not None and self.request.documents:
            raise ValueError("JSON creation cannot claim attachments; use the invoice upload endpoint")
        return self


class ApprovalAccepted(StrictModel):
    request_id: str
    status: ApprovalStatus
    detail_url: str
    events_url: str


class HumanReviewRequest(StrictModel):
    action: HumanReviewAction
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=128)
    recommendation: Recommendation | None = None

    @model_validator(mode="after")
    def validate_edit(self) -> "HumanReviewRequest":
        if self.action == HumanReviewAction.EDIT:
            if self.recommendation not in {
                Recommendation.PASS_RECOMMENDED,
                Recommendation.REJECT_RECOMMENDED,
            }:
                raise ValueError("EDIT requires a pass or reject recommendation")
        elif self.recommendation is not None:
            raise ValueError("only EDIT accepts an explicit recommendation")
        return self


class RuleReplayCase(StrictModel):
    amount: str | None = None
    attendees: int | None = Field(default=None, ge=1)
    tier: str | None = None
    days: int | None = Field(default=None, ge=0)
    expected: Literal["PASS", "FAIL"]


class CandidateApprovalRequest(StrictModel):
    source_document_id: str = Field(min_length=1)
    effective_from: DateOnly
    parameters: dict[str, str | int] = Field(min_length=1)
    replay_cases: list[RuleReplayCase] = Field(min_length=2)
