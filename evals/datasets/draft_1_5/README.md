# 1.5.0 validation 草案

本目录是内部调试集，不是冻结测试集。旧的 1.4.0 报告继续对应原来的 26 条，不能当作本版成绩；`test.freeze.json` 保持不变。本版 60 条已在单公司组合目录下完整运行一次，结果见[原始报告](../../reports/validation_1_5_active_qwen_full.json)和[解读](../../reports/validation_1_5_active_qwen_full.md)。

| 内容 | 数量 | 解释 |
| --- | ---: | --- |
| 审批案例 / Agent 轨迹 | 60 / 60 | 在 1.4 的 26 条基础上增加 34 条 |
| 新增独立交易票据 | 20 份 PDF | 每份有自己的票号、日期、事项和票面字段 |
| 同票据反事实案例 | 14 条 | 改申请金额、日期或人数，**不算**独立票据 |
| 验证集不同票据总数 | 31 份 PDF | 旧 11 份 + 本版 20 份 |
| 检索问句 | 20 条 | 本轮未扩充，沿用 1.4 |

建议标签分布：建议通过 23、建议驳回 23、人工复核 14；交通 26、餐饮 30、住宿 4。新增 20 份票据覆盖餐饮与交通的正常、限额、金额/日期不一致、人数缺失/冲突、行程缺失及超时提交。住宿仍只有原有的 4 条，后续扩容不能把 60 条误说成三类费用均衡。

票据均为项目合成，分表格、收据、记录单三种版式，但仍共用一个 PDF 渲染器；它们是不同交易，不是不同真实开票系统的 OCR 分布。不能用本集声称真实企业准确率。PDF 来源哈希、页码、证据摘录和解析切片写在相邻清单里；其中 6 份旧票据来自 `draft_1_4`，5 份更早的票据来自项目基础语料。

本目录保存原始 1.5.0 金标和当时的完整运行，供对照；`scripts/build_validation_expansion.py` 现在生成[校正后的 1.5.1 草案](../draft_1_5_1/README.md)，复用相同票据 PDF，不覆盖本目录案例。原 1.5.0 的评估命令是：

`uv run --env-file .env python scripts/evaluate_workflow.py --dataset-dir evals/datasets/draft_1_5 --attachment-chunks evals/datasets/draft_1_5/attachment_chunks.jsonl --attachment-evidence evals/datasets/draft_1_5/attachment_evidence.json --split validation --active-catalog --output tmp/validation_1_5_active.json`

评估命令处理的是已解析的 PDF 文本与工作流，**不是**全部原始 PDF 经 MinerU 上传的端到端成绩。留出的 20 份新票据在 [`holdout_candidates_1_5`](../holdout_candidates_1_5/README.md)；目前无人工金标，不能计作 test。

2026-09-29 定向冒烟曾发现 `M02`（应通过）被错误转人工：检索把部门细则第 4 页的 160 元条款排到第 6 位，Reviewer 又只看通用制度冲突 ID。修复后该案例在单例复跑和完整 60 条运行中均严格通过；金标没有因模型表现而修改。全量仍有系统错误和严格轨迹不足，详见报告解读。
