"""The no-Supervisor ablation routes a Reviewer result without another model call."""

from typing import cast

from app.agents.contracts import (
    CoverageStatus,
    EvidenceAction,
    EvidenceReview,
    QuestionCoverage,
    SupervisorAction,
)
from app.agents.supervisor import decide_reviewer_direct
from app.graph.state import ApprovalState


def test_sufficient_review_validates_without_supervisor_model() -> None:
    state = cast(
        ApprovalState,
        {
            "evidence_review": EvidenceReview(
                recommended_action=EvidenceAction.SUFFICIENT,
                reason="关键制度已核实",
                coverage=[
                    QuestionCoverage(
                        question_id="Q1",
                        status=CoverageStatus.SUPPORTED,
                        evidence_ids=["E1"],
                    )
                ],
            )
        },
    )
    decision, tokens, _, used_model, _ = decide_reviewer_direct(state)
    assert decision.action == SupervisorAction.VALIDATE
    assert decision.stop_reason == "ALL_NECESSARY_EVIDENCE_COVERED"
    assert tokens == 0
    assert used_model is False
