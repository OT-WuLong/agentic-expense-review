# 1.5.1 validation 金标校正草案

与 [1.5.0](../draft_1_5/README.md) 相同的 60 条申请、31 份票据 PDF、轨迹约束与制度目录；只修正 `APP-VAL-MEAL-PASS` 和 `APP-VAL-MEAL-MISMATCH` 两条旧案例的 `expected_evidence_ids`。它们原来沿用预算／权限／查重的结构化证据金标，但当前部门餐饮规则不查询这些事实，轨迹也只允许 `policy_search`。移除的是不可达的旧锚点，金额、票据、规则结果和预期建议没有改动。

本目录与原 1.5.0 完整报告分开保存；1.5.1 的[全量重跑结果](../../reports/validation_1_5_1_active_qwen_full.md)已另存，发现一条错误建议通过，尚不能视为收口。票据 Markdown/PDF 继续位于 `draft_1_5/attachments`，本目录的附件清单和切片引用同一批字节；20 条未标注保留集也未变。本版构建脚本见 `b3793af`，当前脚本构建后续 1.5.2 草案。

评估时使用 `--dataset-dir evals/datasets/draft_1_5_1`，附件切片和证据分别指向本目录的 `attachment_chunks.jsonl`、`attachment_evidence.json`，并将输出写到新的报告文件。1.5.0 的 55/60、33/60 与本版 57/60、40/60 必须分别归档，不得混报。

两条校正案例已定向用真实模型重跑：`MEAL-PASS` 严格通过；`MEAL-MISMATCH` 最终按规则驳回，证据和规则均匹配，但 Agent 先以 `REQUEST_DOCUMENTS` 停止，故严格停因仍不匹配。后续报告采用 v2 结构，分别记录阶段停因、最终 `HARD_RULE_FAILED` 判因及“不含停因的诊断性成功”，不会用该诊断值替换严格成绩。此两条结果不代表 60 条全量。
