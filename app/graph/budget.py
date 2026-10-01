"""Single source of truth for hard limits and one-more-round feasibility."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from app.agents.contracts import AgentRole
from app.graph.state import ApprovalState

INITIAL_RETRIEVAL_ROUND_AGENT_STEPS = 2
NEXT_RETRIEVAL_ROUND_AGENT_STEPS = 3


@dataclass(frozen=True, slots=True)
class RoundBudget:
    can_start_round: bool
    start_block_reason: str | None
    remaining_agent_steps: int
    remaining_retrieval_rounds: int
    remaining_query_rewrites: int
    remaining_tokens: int | None
    remaining_deadline_ms: int
    estimated_round_tokens: int
    estimated_round_latency_ms: int
    no_progress_rounds: int

    def payload(self) -> dict[str, object]:
        return asdict(self)


def hard_limit_violation_reason(state: ApprovalState) -> str | None:
    token_limit = state["token_budget"]
    return next(
        (
            reason
            for condition, reason in (
                (state["agent_step_count"] > state["max_agent_steps"], "MAX_AGENT_STEPS"),
                (
                    state["retrieval_round_count"] > state["max_retrieval_rounds"],
                    "MAX_RETRIEVAL_ROUNDS",
                ),
                (
                    state["query_rewrite_count"] > state["max_query_rewrites"],
                    "MAX_QUERY_REWRITES",
                ),
                (
                    token_limit is not None and state["tokens_used"] > token_limit,
                    "TOKEN_BUDGET",
                ),
                (datetime.now(UTC) >= state["deadline_at"], "DEADLINE"),
            )
            if condition
        ),
        None,
    )


def evaluate_round_budget(state: ApprovalState) -> RoundBudget:
    recent_agent_steps = [
        step
        for step in reversed(state.get("agent_trajectory", []))
        if step.agent
        in {AgentRole.SUPERVISOR, AgentRole.RETRIEVAL, AgentRole.EVIDENCE_REVIEWER}
    ][:NEXT_RETRIEVAL_ROUND_AGENT_STEPS]
    selected_count = len(state.get("selected_tools", []))
    recent_observations = (
        state.get("tool_observations", [])[-selected_count:] if selected_count else []
    )
    # Reserve 50% for growing evidence and call history in later rounds.
    estimated_tokens = max(1, sum(step.tokens_used for step in recent_agent_steps) * 3 // 2)
    estimated_latency_ms = max(
        1,
        sum(step.latency_ms for step in recent_agent_steps)
        + sum(item.latency_ms for item in recent_observations),
    )
    remaining_steps = max(0, state["max_agent_steps"] - state["agent_step_count"])
    remaining_rounds = max(
        0, state["max_retrieval_rounds"] - state["retrieval_round_count"]
    )
    remaining_rewrites = max(
        0, state["max_query_rewrites"] - state["query_rewrite_count"]
    )
    token_limit = state["token_budget"]
    remaining_tokens = (
        None if token_limit is None else max(0, token_limit - state["tokens_used"])
    )
    remaining_deadline_ms = max(
        0, round((state["deadline_at"] - datetime.now(UTC)).total_seconds() * 1000)
    )
    required_steps = (
        INITIAL_RETRIEVAL_ROUND_AGENT_STEPS
        if state.get("evidence_review") is None
        else NEXT_RETRIEVAL_ROUND_AGENT_STEPS
    )
    start_block_reason = next(
        (
            reason
            for condition, reason in (
                (
                    remaining_steps < required_steps,
                    "INSUFFICIENT_AGENT_STEPS_FOR_ROUND",
                ),
                (remaining_rounds <= 0, "MAX_RETRIEVAL_ROUNDS"),
                (
                    state["retrieval_round_count"] > 0 and remaining_rewrites <= 0,
                    "MAX_QUERY_REWRITES",
                ),
                (
                    remaining_tokens is not None
                    and remaining_tokens < estimated_tokens,
                    "PROJECTED_TOKEN_BUDGET",
                ),
                (
                    remaining_deadline_ms < estimated_latency_ms,
                    "PROJECTED_DEADLINE",
                ),
                (state["no_progress_rounds"] >= 2, "NO_NEW_EVIDENCE"),
            )
            if condition
        ),
        None,
    )
    return RoundBudget(
        can_start_round=start_block_reason is None,
        start_block_reason=start_block_reason,
        remaining_agent_steps=remaining_steps,
        remaining_retrieval_rounds=remaining_rounds,
        remaining_query_rewrites=remaining_rewrites,
        remaining_tokens=remaining_tokens,
        remaining_deadline_ms=remaining_deadline_ms,
        estimated_round_tokens=estimated_tokens,
        estimated_round_latency_ms=estimated_latency_ms,
        no_progress_rounds=state["no_progress_rounds"],
    )
