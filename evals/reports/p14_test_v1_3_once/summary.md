# P14 evaluation summary

- Run: `P14-20260925T122306Z-72af3881`
- Commit: `d9bfee4e942eea4c85116614106700068982968f` (dirty=False)
- Split: `test`; model: `deepseek-flash`
- Raw predictions and full configuration are in the adjacent JSON files.
- The tiny synthetic sample is descriptive, not a population estimate.

| Suite | Metric | Value | Support | Status |
| --- | --- | ---: | ---: | --- |
| retrieval | `retrieval.recall_at_5` | 1.000 | 3 | MEASURED |
| retrieval | `retrieval.mrr` | 1.000 | 3 | MEASURED |
| retrieval | `retrieval.ndcg_at_10` | 1.000 | 3 | MEASURED |
| retrieval | `retrieval.no_answer_accuracy` | — | 1 | NOT_MEASURED |
| retrieval | `retrieval.filter_violation_rate` | 0.000 | 44 | MEASURED |
| workflow | `agent.task_success_rate` | 0.667 | 3 | MEASURED |
| workflow | `agent.subquestion_coverage` | 1.000 | 3 | MEASURED |
| workflow | `agent.mean_steps` | 2.333 | 3 | MEASURED |
| workflow | `agent.tool_selection_case_accuracy` | 1.000 | 3 | MEASURED |
| workflow | `approval.accuracy` | 1.000 | 3 | MEASURED |
| workflow | `approval.macro_f1` | 1.000 | 3 | MEASURED |
| workflow | `approval.false_auto_pass_rate` | 0.000 | 2 | MEASURED |
| workflow | `approval.rule_result_exact_match_rate` | 0.667 | 3 | MEASURED |
| workflow | `approval.human_intervention_rate` | 0.333 | 3 | MEASURED |
| workflow | `approval.system_error_rate` | 0.000 | 3 | MEASURED |
| security | `security.attack_objective_success_rate` | 0.000 | 21 | MEASURED |
| security | `security.benign_false_block_rate` | 0.000 | 7 | MEASURED |

## Suite execution

- retrieval: exit=0; report=F:\乌龙\code\vs_code\简历项目\evals\reports\p14_test_v1_3_once\retrieval.json
- workflow: exit=0; report=F:\乌龙\code\vs_code\简历项目\evals\reports\p14_test_v1_3_once\workflow.json
- security: exit=0; report=F:\乌龙\code\vs_code\简历项目\evals\reports\p14_test_v1_3_once\security.json

## Frozen threshold checks

- `retrieval.recall_at_5`: 1.0 >= 0.8 → PASS
- `retrieval.filter_violation_rate`: 0.0 == 0.0 → PASS
- `agent.task_success_rate`: 0.6666666666666666 >= 0.3 → PASS
- `approval.accuracy`: 1.0 >= 0.6 → PASS
- `approval.macro_f1`: 1.0 >= 0.5 → PASS
- `approval.rule_result_exact_match_rate`: 0.6666666666666666 >= 0.6 → PASS
- `approval.false_auto_pass_rate`: 0.0 == 0.0 → PASS
- `approval.system_error_rate`: 0.0 == 0.0 → PASS
- `security.attack_objective_success_rate`: 0.0 == 0.0 → PASS
- `security.benign_false_block_rate`: 0.0 == 0.0 → PASS
