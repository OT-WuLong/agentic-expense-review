# P14 1.3.0 保留集冻结前记录

- 旧 `1.2.0` test 在提交 `080fe29` 上仅运行过一次，结果为 `INVALID_RUN`；原始报告继续保存在 `p14_test_once/`，旧 test 行移至 `evals/datasets/archive/1.2.0/`，没有在修复后重跑。
- 未找到兼具完整申请、适用制度、结论和明确再发布许可的普通企业逐单样本。新 test 由项目自行编写：3 份虚构票据、3 单审批（PASS／REJECT／HUMAN 各一）、4 条检索（其中一条无答案）和一组恶意／良性工具参数。公开参考资料及合成标注见 `data/fixtures/source_manifest.json`。这不是独立外部盲测，也不能据此推断真实企业准确率。
- 新申请对应现有合成企业甲的已发布部门细则；没有导入真实企业条款原文或真实人员、交易。三个新 PDF 经 MinerU 解析并加入 P04 切片，实际抽取到的日期、金额和票据号与作者标注一致。参考制度的交通票据／时限、住宿限额／时限、餐饮人均限额已进入 PostgreSQL 规则目录，避免把“缺规则”误当成模型证据不足。
- 使用相同 `1.3.0` validation、Dense 检索和 `deepseek-flash` 的完整运行见 `p14_validation_v1_3_deepseek/`：检索 Recall@5 = 3/3，过滤违规 0/25；审批建议 3/3、规则结果 3/3、严格任务成功 2/3、系统错误 0/3；安全攻击成功 0/21、良性误拦 0/7。剩余一条 validation 的检索与规则均正确，但 Supervisor 写“证据已覆盖”，金标只接受“人工例外／补材料”停因，因此严格任务成功不计。保留负结果，不为分数修改金标。
- Qwen Agent 的 validation 尝试出现 `ProxyError`、`SSLError`，另一轮出现查询超时；据此选择已完成同批 validation 的 DeepSeek 冻结配置。失败中间产物保留在本机忽略目录 `tmp/p14/p14_validation_v1_3_*`，不与正式验证报告合并。
- 阈值与运行配置已写入 `evals/datasets/thresholds.freeze.json`，新 test 只允许在代码与数据提交且工作区干净后只读运行一次。此次 3 单规模只足以做版本化回归；Qwen 与 DeepSeek 网络波动、无答案处理泛化、相关案例记忆收益仍不能据此证明。
