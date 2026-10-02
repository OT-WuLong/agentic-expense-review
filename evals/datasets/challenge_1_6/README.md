# 1.6.0 中高难 validation 草案

新增 40 笔合成交易：20 条 `MEDIUM`、20 条 `HIGH`；住宿 16 条、餐饮 12 条、交通 12 条。预期建议为通过 15 条、驳回 9 条、转人工 16 条。难度表示业务设计复杂度，并非模型实测难度。每笔交易有独立发票，另有 18 份补充材料，共 58 份可上传 PDF。

[逐案清单](case-index.md)列出判定理由，[机器索引](case_index.json)列出 PDF 路径与制度证据 ID。主要覆盖：

- 城市等级调整前后、发生日与提交日的不同时间口径；间夜数、客房数及部门标准的联合计算。
- 餐饮新旧版本重叠：两版结论分歧、两版均通过、两版均超限；到期日与到期次日；部门细则优先适用。
- 实际支付与原价/优惠/退款的区分；票据事实优先于申请人描述；有原始发票但缺实际路线或人数。
- 实际入住水单补齐发票日期；行程计划和部门便笺不能替代已发生事实及权威记录。
- 预算、员工权限、票据查重暂缺或异常；会议指定酒店和延迟提交的人工裁量边界。

## 数据与金标

`approvals.jsonl` 包含申请、手工编写的抽取金标、规则结果、风险项和建议；`agent_trajectories.jsonl` 描述必要证据与允许工具；`retrieval.jsonl` 用于制度检索。`attachment_manifest.json` 关联作者稿和 PDF，`attachment_chunks.jsonl` 保存预解析附件，`attachment_evidence.json` 同时保存附件、制度和结构化证据锚点。

制度不另造一套，沿用当前单公司发布目录。制度锚点的 `excerpt` 逐字取自实际入库切片，`authoring_excerpt` 保留 Markdown 原条款，便于辨认解析换行或 OCR 差异。申请地点、人数、日期等应知事实写在申请和票据中；不会将预期答案或金标 ID 注入 Agent。

`structured_snapshots.json` 有 40 个独立评测快照、131 条合成城市/员工/预算/权限/查重/客户拜访记录。同部门同月的预算不互相覆盖；缺失记录确实不写入，未来记录仅在生效后可读。这些不是实时企业财务数据，查重为指定场景快照，不等同于跨请求实时去重。

补充通知和批准记录标为 `OTHER`：进入附件证据审阅，但不套发票字段抽取器。酒店实际水单使用现有 `ITINERARY` 日期抽取接口，其正文明确区分实际入住与计划行程。H14 虽有书面超标例外记录，仍以当前 MVP 的人工复核边界标为转人工，不把“有例外材料”写成普通额度内通过。

金标按已有制度与交易事实编写，不依据系统预测反填。标签为 `DRAFT_REVIEWABLE`，尚未经独立人员复核；全部属于开发可见的合成 validation，不是外部盲测。相关情景共享条款族，不能把 40 笔发票等同于 40 个统计独立问题。原冻结 test 和已有 60 条数据不变。

## 生成与运行

只生成本地数据与 PDF，不调用模型、不修改数据库：

```powershell
uv run python scripts/build_challenge_dataset.py
```

启动已有 PostgreSQL 和 Milvus、完成原项目迁移与入库后，可仅加入新场景的隔离结构化快照：

```powershell
uv run python scripts/build_challenge_dataset.py --seed
```

`--seed` 不创建审批单、不修改原公司或演示快照。之后使用原工作流评测器，金标仍只供评分：

```powershell
$env:AGENT_MODEL = 'deepseek-flash'
$env:POLICY_RETRIEVAL_MODE = 'dense'
uv run python scripts/evaluate_workflow.py --split validation --active-catalog --dataset-dir evals/datasets/challenge_1_6 --attachment-chunks evals/datasets/challenge_1_6/attachment_chunks.jsonl --attachment-evidence evals/datasets/challenge_1_6/attachment_evidence.json --variant multi_agent --output evals/reports/challenge_1_6_multi_dense_first.json
```

输出文件不能已存在，失败样本也保留在分母中。已完成 [40 条单 Agent / 多 Agent 真实模型对照](../../reports/single_vs_multi_dense_challenge_1_6_deepseek_20261002.md)：建议匹配率为 75.0% / 95.0%，业务完整成功率为 30.0% / 37.5%，严格任务成功率为 25.0% / 27.5%。两组均有餐饮同结论版本误升级问题；引用完整性及金标等价来源口径也待完善，未修改本轮标签或分数。预解析工作流评测不等同于 58 次 MinerU 上传解析。

这些 PDF 可以用于人工查看和上传解析，但本组预期结果绑定相应申请、部门及独立评测快照。直接用网页中的演示员工上传相同 PDF，并不具备相同预算、权限和城市快照；不能要求其输出与本组金标一致。

## 后续对照口径

40 条新样本应单独报告，再报告与原 60 条合并后的 100 条整体结果。主要比较建议匹配率和业务完整成功率，并列出错误自动通过和系统错误；不要求固定调用顺序、强制挑战或固定检索轮数。

新场景允许最多三轮检索、两次改写，仍受步骤、Token 和截止时间约束。已有 `single_agent_dense` 是单轮基线；直接与它比较只能说明“单轮方案 vs 完整方案”，不能把多轮检索的收益全部归因于多 Agent。若要单独衡量协作收益，需要另加允许同样多轮、同样工具与预算的单 Agent 对照。混合检索收益也应与 Agent 架构收益分开报告。
