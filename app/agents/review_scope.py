"""Server-owned questions for evidence review, scoped to the published catalogue."""

from app.agents.contracts import SubQuestion, ToolName
from app.graph.state import ApprovalState
from app.models import ExpenseType
from app.tools.registry import ToolExecutionContext


def required_questions(state: ApprovalState, context: ToolExecutionContext) -> list[SubQuestion]:
    application = state["application"]
    policy_ids = "、".join(context.allowed_document_ids) or "无"
    rule_ids = "、".join(context.applicable_rule_ids) or "无"
    rule_boundary = (
        f"本单已发布且适用的必核规则仅有：{rule_ids}；不得把清单外的限额或例外当作缺口。"
        if context.rule_scope_complete
        else "只核对有制度原文依据的要求，不得凭空要求限额或例外。"
    )
    questions = [
        SubQuestion(
            question_id="Q-POLICY",
            text=(
                f"核实发生日与部门适用的已发布制度（{policy_ids}）中，本笔{application.expense_type.value}"
                f"的可报销条件、明确禁止项及已规定的额度或版本冲突。{rule_boundary}"
            ),
        ),
        SubQuestion(
            question_id="Q-RECEIPT",
            text="核对已上传票据的费用类别、日期、金额及制度明示要求的业务事实与申请是否一致；两侧字段明确但不一致交规则判断，缺件才补材料。",
        ),
    ]
    if application.expense_type == ExpenseType.LODGING and "会议" in application.description:
        questions.append(
            SubQuestion(
                question_id="Q-EXCEPTION",
                text=(
                    "申请提及会议酒店。核查适用的住宿制度是否存在超标准例外，"
                    "以及所需会议材料和批准角色；若无原文依据，不得自行创造例外。"
                ),
            )
        )
    if (
        application.expense_type == ExpenseType.LODGING
        and ToolName.STRUCTURED_LOOKUP in context.allowed_tools
        and "city_tier" in context.allowed_structured_query_types
    ):
        questions.append(
            SubQuestion(
                question_id="Q-CITY-TIER",
                text="取得入住日期的城市等级，并用该等级核对已发布住宿制度中明确的每间夜标准。",
            )
        )
    return questions
