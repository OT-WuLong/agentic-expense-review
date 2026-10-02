# 1.6.1 中高难案例：标签契约修订

沿用 [1.6.0](../challenge_1_6/README.md) 的 40 笔交易、58 份凭证和 131 条隔离结构化记录，不修改申请事实、预期建议、规则结果、风险标签或预算。PDF 和作者稿仍引用原路径，没有另造发票。原数据及其真实对照报告保留不变。

本版仅修订两类评分约束，详见 [label_changes.json](label_changes.json)：

- 部门住宿场景允许通用城市等级条款或部门第十九条作为等价依据：两者均要求采用住宿发生日有效的财务分级。独立的带日期城市记录仍为必需证据，不能凭部门条款猜等级。额度、计算公式与提交期限的证据要求不放宽。
- 会议酒店例外、重复票据和逾期人工场景，允许检索已经完成、随后由规则转人工的停止路径。仅改变合法停因集合，不把转人工改为通过。

`acceptable_evidence_groups` 中每组 `any_of` 至少命中一个来源；组外 `expected_evidence_ids` / `required_evidence_ids` 仍要求全部命中。评测器同时检查对应子问题的证据覆盖。等价条款仍须通过原有权限、日期与来源闸门。

修订脚本 `scripts/revise_challenge_labels.py` 只读取制度与原场景定义，不读取模型预测；仍标为开发可见、未独立复核的合成 validation。不会改冻结 test，也不会回填旧报告分数。合并的 100 条目录为 `draft_1_6_1`，其中原 60 条标签原样保留。

新运行须使用本目录的 `approvals.jsonl`、`agent_trajectories.jsonl`、`attachment_chunks.jsonl` 和 `attachment_evidence.json`，另选一个不存在的报告路径。局部修复复验不代表 40/100 条全量结果。
