# 1.4.0 validation 草案

本目录是可调参的候选集，不是冻结 test；其内部评测结果不能当作独立盲测或真实企业准确率。原有 1.3.0 数据与 `test.freeze.json` 未改动。

- 审批 26 条：交通 11、餐饮 11、住宿 4；建议通过 9、建议驳回 9、转人工 8。
- 对应 Agent 轨迹 26 条；制度检索问题 20 条，其中无答案 4 条。
- 26 条审批使用 11 份合成票据 PDF，其中 6 份是本批新增的独立票据；其余同票据字段扰动属于边界回归，不是独立单据。
- 1.2.0 已使用过的三份旧 test 票据在此明确降为 validation；不能再作为盲测使用。

修改票据 Markdown 后，先运行 `uv run python scripts/build_fixture_pdfs.py --source-dir evals/datasets/draft_1_4/attachments`，再运行 `uv run python scripts/build_validation_draft.py`。后者从实际 PDF 文本生成新附件切片、来源清单和证据锚点，并沿用现有数据集校验；不会改原有语料和冻结文件。

跑审批草案：`uv run --env-file .env python scripts/evaluate_workflow.py --dataset-dir evals/datasets/draft_1_4 --attachment-chunks evals/datasets/draft_1_4/attachment_chunks.jsonl --attachment-evidence evals/datasets/draft_1_4/attachment_evidence.json --split validation --output tmp/validation_1_4_workflow.json`。这条命令评测的是已解析票据文本进入 Agent 后的流程，不等同于 MinerU 上传端到端测试。

要与网页新申请使用相同的单公司组合制度目录，在上述命令中加入 `--active-catalog`，并另存输出文件。报告会分别记录样本原始来源快照和实际运行的组合目录；结构化事实仍使用该合成样本声明的参考快照，不能冒充网页上传后的实时查重。此选项不改动 1.4 样本锚点或冻结 test。未加参数时仍沿用原来的参考制度快照，不能将两种运行口径混报。

2026-09-29 本地 BM25 对 16 条有答案问句试跑：Recall@5 = 0.9375、nDCG@10 = 0.8541；4 条无答案问句当前评测器不计准确率。Dense 在新增多证据问句后的重跑遇到 DashScope TLS 断连，因此没有当前草案的 Dense 成绩。这些数字仅用于筛查草案，不是冻结 test 成绩。

通过案由 3 条补到 9 条。六份新增票据的定向真实模型运行与两次原始 PDF 上传见[流程报告](../../reports/v14_invoice_real_flow_2026-09-29.md)。按当前单公司组合目录和 `qwen3.8-flash` 完整运行 26 条后的[原始报告](../../reports/validation_1_4_active_qwen_postfix_full.json)记录：建议匹配 24/26（92.3%）、严格任务成功 14/26（53.8%），6 份新增独立票据 6/6 严格通过，1 条模型 TLS 系统错误计入分母。严格任务同时检查证据、规则、风险标记、子问题、工具能力和停因；这仍只是内部 validation。随后应另写不复用上述 PDF、模板细节和票号的新 test，并升版冻结。
