"""Construction helpers for the fixed v1 read-only tool registry."""

from app.agents.contracts import ToolName
from app.structured import StructuredRecordFinder
from app.tools.case_search import CaseSearchTool
from app.tools.policy_search import PolicySearchTool
from app.tools.registry import ToolExecutionContext, ToolRegistry, ToolSpec
from app.tools.rule_search import RuleSearchTool
from app.tools.structured_lookup import StructuredLookupTool


# 构建 v1 只读工具注册表：注入制度索引与结构化记录仓库，并绑定超时与结果上限
def build_tool_registry(
    policy_index: object,
    structured_store: StructuredRecordFinder,
) -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec(
                name=ToolName.POLICY_SEARCH,
                handler=PolicySearchTool(policy_index),
                timeout_seconds=90,
                max_results=5,
            ),
            ToolSpec(
                name=ToolName.STRUCTURED_LOOKUP,
                handler=StructuredLookupTool(structured_store),
                timeout_seconds=1,
                max_results=1,
            ),
            ToolSpec(
                name=ToolName.CASE_SEARCH,
                handler=CaseSearchTool(
                    structured_store, getattr(policy_index, "embedder", None)
                ),
                timeout_seconds=25,
                max_results=3,
            ),
            ToolSpec(
                name=ToolName.RULE_SEARCH,
                handler=RuleSearchTool(structured_store),
                timeout_seconds=2,
                max_results=5,
            ),
        ]
    )


__all__ = [
    "ToolExecutionContext",
    "ToolRegistry",
    "build_tool_registry",
]
