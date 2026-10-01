# 1.5.2 validation 全量重跑（2026-09-30）

[逐案原始报告](validation_1_5_2_active_qwen_full.json)是 `qwen3.8-flash`、Dense 检索、单公司组合制度目录上的一次完整 60 条合成审批运行。使用 [1.5.2 草案](../datasets/draft_1_5_2/README.md)；系统错误和错判均留在分母内。与 1.5.1 相比，票据字节未改，但 7 条人数相关规则金标发生了版本化变化。此运行输入是已解析的附件文本，不等于 60 份原始 PDF 都经过网页上传。

| 指标 | 结果 |
| --- | ---: |
| 建议匹配 | 59/60（98.3%） |
| 严格任务成功（含证据、规则、工具能力与 Agent 停因） | 37/60（61.7%） |
| 不含 Agent 停因的诊断成功 | 51/60（85.0%），不能替代严格成功率 |
| Agent 阶段停因匹配 | 39/60（65.0%） |
| 规则结果精确匹配 | 59/60（98.3%） |
| 错误自动建议通过 | 0/37（本次 validation 的非通过金标样本） |
| 系统错误 | 1/60（1.7%） |

20 份新编票据的申请建议匹配 20/20、严格成功 14/20；14 条同票据字段变体建议匹配 14/14、严格成功 8/14。原有 26 条建议匹配 25/26、严格成功 15/26。样本均为项目合成验证，不是独立盲测或真实企业准确率。

原先两条业务错判已纠正：`APP-VAL-MEAL-HEADCOUNT-CONFLICT` 抽取出票据 1 人、申请 2 人，`RULE-MEAL-DOCUMENT-CONSISTENCY` 判 `INDETERMINATE`，最终建议人工；`APP-VAL-COMMUTE-FALSE-PURPOSE` 的票据明确为住所至固定办公地点，`RULE-SALES-PRIVATE-COMMUTE` 判 `FAIL`，最终建议驳回。后者严格任务仍失败：这一轮未命中金标指定的销售部禁报证据 ID，Agent 停因也未匹配，不能把业务建议正确等同于证据完整。

另用部署中的 HTTP 上传接口、原始合成 PDF 和 MinerU 解析各跑一单（容器模型为 `deepseek-flash`，与上表的 Qwen 评测分开）：本机 `REQ-LIVE-SAFETY-4e45073898eb` 抽取 `attendee_count=1`，以 `ATTENDEE_COUNT_CONFLICT` 转人工；`REQ-LIVE-SAFETY-d4c00bb7d385` 以 `CONFIRMED_PRIVATE_COMMUTE` 建议驳回。这两笔仅保存在本机演示数据库，不属于上述 60 条分数。

唯一的建议不匹配是 `APP-VAL-MEAL-AMOUNT-CORRECTED`：Retrieval Agent 在首次工具调用前出现 `ValueError`，工作流返回 `SYSTEM_ERROR`，原始报告未用其他运行补齐。严格成功从 1.5.1 的 40/60 变成 37/60，不能宣称全面提升；本轮主要证明错误自动通过由 1 条降为 0 条，Agent 停因和定向证据召回仍是明确短板。不同金标版本与模型随机性也使两轮总分不宜作严格因果比较。
