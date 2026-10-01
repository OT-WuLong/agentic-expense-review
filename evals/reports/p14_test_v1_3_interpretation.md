# P14 1.3.0 一次性 test 结果解读

- 冻结代码提交：`d9bfee4e942eea4c85116614106700068982968f`；运行 ID：`P14-20260925T122306Z-72af3881`；模型：`deepseek-flash`；检索：Dense；`manifest.json` 记录 `dirty=false`、`MEASURED`、`run_outcome=PASS`。本版本 test 只运行了这一次，完整原始预测、阈值判定与控制台输出保存在同目录 `p14_test_v1_3_once/`。
- 预先冻结的 10 项阈值全部通过：有答案检索 Recall@5 为 3/3，过滤违规 0/44；审批建议 3/3、Macro-F1 1.0、规则结果精确匹配 2/3、严格 Agent 任务成功 2/3、错误自动通过 0/2、系统错误 0/3；安全脚本的攻击目标达成 0/21、良性误拦 0/7。这些分母很小，安全项还混合了边界检查，不能解释为真实环境防护率。
- `APP-HO-MEAL-PASS` 与 `APP-HO-TAXI-REJECT` 的证据、规则、建议和停因全部匹配。`APP-HO-MEAL-HEADCOUNT` 正确给出 `HUMAN_REVIEW`／`INSUFFICIENT_EVIDENCE`，并明确请求实际参加人数；但 Reviewer→Supervisor 在补材料节点结束，未运行后置 Rule Validator。该样本的作者金标要求 `RULE-OPS-MKT-MEAL-LIMIT=INDETERMINATE`，因此规则匹配和严格任务成功为 false。不得把 3/3 建议正确宣传成 3/3 端到端任务成功，也不应根据这次 test 更改同版本金标后重跑。
- 无答案查询有 1 条，但 `retrieval.no_answer_accuracy` 在现有评测器中标为 `NOT_MEASURED`，不能从 Recall@5 推断系统已学会拒答。案例记忆命中后的收益、实际生产网络稳定性、独立外部盲测以及总体 P95 仍无可用证据。
- 新 test 是开发者参考公开制度自行编写的合成保留集，不是真实企业逐单数据，也不是独立出题的盲测；公开参考资料及合成标注见 `data/fixtures/source_manifest.json`。旧 `1.2.0` 的唯一一次 `INVALID_RUN` 继续单独保留在 `p14_test_once/`，没有重跑或与本次成绩合并。
