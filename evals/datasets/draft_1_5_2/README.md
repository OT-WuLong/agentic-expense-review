# 1.5.2 validation 安全边界修正草案

沿用 1.5.1 的 60 条合成申请和同一批 31 份 PDF，不改票据字节。餐饮票据中原本可见、却未进入结构化抽取的参加人数现在纳入预期字段；7 条“申请人数缺失或与票据不一致”的案例相应将 `RULE-MEAL-DOCUMENT-CONSISTENCY` 标为 `INDETERMINATE`，避免自动通过。`APP-VAL-COMMUTE-FALSE-PURPOSE` 的金标不变：票据明确为日常通勤时应建议驳回，补件请求不能覆盖明确禁报。

本草案由 `uv run python scripts/build_validation_expansion.py` 生成。使用 `--dataset-dir evals/datasets/draft_1_5_2` 评测，附件切片及证据均取本目录同名文件，票据 PDF 仍在 `draft_1_5/attachments`。[1.5.2 全量结果](../../reports/validation_1_5_2_active_qwen_full.md)和 [1.5.1 历史结果](../../reports/validation_1_5_1_active_qwen_full.md)分别存档，不能把两个版本的数字混用。
