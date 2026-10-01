# P14 evaluation summary

- Run: `P14-20260925T111538Z-a60ea863`
- Commit: `080fe29e38ca5a66c88103a974f772b81b02b83e` (dirty=False)
- Split: `test`; model: `qwen3.8-flash`
- Raw predictions and full configuration are in the adjacent JSON files.
- The tiny synthetic sample is descriptive, not a population estimate.

| Suite | Metric | Value | Support | Status |
| --- | --- | ---: | ---: | --- |
| retrieval | `retrieval.recall_at_5` | 1.000 | 2 | MEASURED |
| retrieval | `retrieval.mrr` | 1.000 | 2 | MEASURED |
| retrieval | `retrieval.ndcg_at_10` | 0.925 | 2 | MEASURED |
| retrieval | `retrieval.no_answer_accuracy` | — | 1 | NOT_MEASURED |
| retrieval | `retrieval.filter_violation_rate` | 0.000 | 33 | MEASURED |
| workflow | `agent.task_success_rate` | 0.000 | 3 | MEASURED |
| workflow | `agent.subquestion_coverage` | 1.000 | 3 | MEASURED |
| workflow | `agent.mean_steps` | 2.667 | 3 | MEASURED |
| workflow | `agent.tool_selection_case_accuracy` | 1.000 | 3 | MEASURED |
| workflow | `approval.accuracy` | 0.000 | 3 | MEASURED |
| workflow | `approval.macro_f1` | 0.000 | 3 | MEASURED |
| workflow | `approval.false_auto_pass_rate` | 0.000 | 2 | MEASURED |
| workflow | `approval.rule_result_exact_match_rate` | 0.000 | 3 | MEASURED |
| workflow | `approval.human_intervention_rate` | 1.000 | 3 | MEASURED |
| workflow | `approval.system_error_rate` | 0.000 | 3 | MEASURED |

## Suite execution

- retrieval: exit=0; report=F:\乌龙\code\vs_code\简历项目\evals\reports\p14_test_once\retrieval.json
- workflow: exit=0; report=F:\乌龙\code\vs_code\简历项目\evals\reports\p14_test_once\workflow.json
- security: exit=1; report=none

## Frozen threshold checks

- `retrieval.recall_at_5`: 1.0 >= 0.8 → PASS
- `retrieval.filter_violation_rate`: 0.0 == 0.0 → PASS
- `agent.task_success_rate`: 0.0 >= 0.3 → FAIL
- `approval.accuracy`: 0.0 >= 0.6 → FAIL
- `approval.macro_f1`: 0.0 >= 0.5 → FAIL
- `approval.false_auto_pass_rate`: 0.0 == 0.0 → PASS
- `approval.system_error_rate`: 0.0 == 0.0 → PASS
- `security.attack_objective_success_rate`: None == 0.0 → FAIL
- `security.benign_false_block_rate`: None == 0.0 → FAIL
