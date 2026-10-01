# 单 Agent Dense RAG 与多 Agent：60 条合成验证对照

[汇总原始数据](single_vs_multi_dense_1_5_2_deepseek_20261002.json) · [自动汇总表](single_vs_multi_dense_1_5_2_deepseek_20261002.md) · [逐案预测与运行计划](single_vs_multi_dense_1_5_2_deepseek_20261002_raw/)

## 结果

| 指标 | 单 Agent + Dense | 多 Agent + Dense | 多 Agent 差值 |
| --- | ---: | ---: | ---: |
| 建议匹配率 | 57/60（95.0%） | 59/60（98.3%） | +3.3 个百分点 |
| 业务完整成功率 | 54/60（90.0%） | 56/60（93.3%） | +3.3 个百分点 |
| 原合同严格成功率 | 47/60（78.3%） | 50/60（83.3%） | +5.0 个百分点 |
| 错误自动建议通过 | 0/37 | 0/37 | 无差异 |
| 系统错误 | 1/60 | 0/60 | 本轮少 1 条 |
| 非系统错误尝试的机器耗时 P50 | 29.432 秒 | 21.416 秒 | 本轮低 27.2% |
| 非系统错误尝试的机器耗时 P95 | 86.928 秒 | 68.294 秒 | 本轮低 21.4% |
| 每次尝试已记录 Agent token 均值 | 10,225 | 10,922 | 约 +6.8% |

这里的“建议匹配”是预测与金标一致，包括正确驳回和正确转人工，不是申请被批准的比例。

业务完整成功要求建议、最终关键证据、规则结果、风险项和工具授权全部正确，不要求特定工具调用次数或停因标签。严格成功继续采用原评测合同，还要求必要子问题、工具能力和停因匹配；它对单轮架构有额外轨迹要求，因此作为次要指标保留。

## 怎么比较

- 运行代码为提交 `149cdf6882ecad221d3c1194d8a5065c89eb0d9e`，运行期间未修改代码或金标。汇总中的 `dirty=True` 来自新生成、尚未提交的结果目录。
- 两组均使用 `deepseek-flash`、temperature=0、同一当前单公司目录、同一个 Dense 索引、Top-K=5、同一套 P04 字段抽取、来源校验、授权范围和确定性规则。各案例的预算沿用同一轨迹文件。
- 单 Agent 基线采用一个统一的系统 Prompt：先自主规划最多两次授权只读查询，再判断检索结果。两个 LLM 阶段属于同一个逻辑 Agent；原始轨迹里的 RETRIEVAL / EVIDENCE_REVIEWER 是复用的阶段序列化标签。基线只有一轮检索，没有独立 Supervisor 或补检循环；结构化查询权限与完整方案一致。
- 多 Agent 使用生产流程的 Retrieval、Reviewer 和 Supervisor。两组每次只执行一个案例，固定先运行基线再运行多 Agent，每组各完整运行一次 60 条，没有选择最好的一次或重跑失分案例后拼接。
- 输入是 1.5.2 的预解析合成票据切片，沿用同一批 31 份 PDF，60 条包含同票据字段变体。这是自建、已用于开发的 validation，不是 60 笔独立真实企业交易，也不是原始 PDF 上传/OCR 端到端对照。

## 改善与退步发生在哪里

业务完整成功的配对结果为 **3 条改善、1 条退步、净增 2 条**：

| 案例 | 单 Agent | 多 Agent | 原始记录说明 |
| --- | --- | --- | --- |
| `APP-VAL-HOTEL-ROOM-CONFLICT` | 正确转人工，但关键证据不全 | 正确转人工且证据完整 | 多 Agent 首轮取得会议酒店例外条款，严格成功 |
| `APP-VAL-V14-TAXI-01` | 缺提交期限条款而转人工 | 建议通过 | 最终规则与关键证据通过；Reviewer 的工具证据中仍未找到期限原文，不能据此宣称所有语义判断都有完整支持 |
| `APP-VAL-V14-TAXI-02` | 缺提交期限条款而转人工 | 建议通过 | 多 Agent 首轮检出了交通制度第二十九条“30 个日历日内提交” |
| `APP-VAL-V15-M10` | 建议通过 | 请求补地点 | 按既有 PASS 金标失分；部门制度第三十条确实要求申请填写地点，而该申请与票据未提供地点，金标与当前必核规则边界存在待复核点，本轮未改分 |

建议匹配净增 2 条中，一条来自基线的 DeepSeek 读取超时：`APP-VAL-DEADLINE-EXCEPTION` 没有业务结果，仍计入基线 60 条分母。其余两笔交通由转人工变为通过，同时多 Agent 在 M10 失去一笔通过。只作诊断地查看两组都产生业务结果的 59 条，匹配为 57/59 与 58/59，差值约 1.7 个百分点；这个诊断不替代主表。

**多 Agent 的 60 条全部只检索一轮。** 因而本轮只能描述整个 Prompt/职责分工方案相对该单轮基线的小幅差异，不能把收益归因于 Supervisor 补检循环、挑战机制或多跳恢复，也没有测量“幻觉抑制率”。

## 使用这些数字的边界

建议匹配和业务完整成功有小幅改善，但样本小、包含关联变体，而且只有一次、固定顺序运行。延迟差异可能受网络、服务端负载和 Prompt 输出长度影响，不能写成稳定的性能提升。Token 是已记录量的下界，未包含失败模型调用和 Embedding；货币成本没有测量。

可以描述为：“在 60 条自建合成验证案例上，与单轮单 Agent + Dense RAG 基线对比，建议匹配率由 95.0% 达到 98.3%，业务完整成功率由 90.0% 达到 93.3%，37 条非通过案例均未错误自动通过。”需要保留“合成验证案例”和“单轮基线”，不能写成真实企业准确率或独立证明多 Agent 架构的普遍优势。

原来的 [60/60、51/60 单独运行](validation_1_5_2_active_deepseek_postfix_full.md)继续作为历史记录保留，本轮对照使用新测出的 59/60、50/60，不能拿历史最好成绩与新基线相减。

## 复现命令

配置自己的 `.env` 并启动 PostgreSQL、Milvus 后，在仓库根目录执行；输出路径必须选未使用的新名字。

```powershell
$env:AGENT_MODEL = 'deepseek-flash'
$env:POLICY_RETRIEVAL_MODE = 'dense'
uv run --env-file .env python scripts/evaluate_agentic_ablation.py --split validation --dataset-dir evals/datasets/draft_1_5_2 --attachment-chunks evals/datasets/draft_1_5_2/attachment_chunks.jsonl --attachment-evidence evals/datasets/draft_1_5_2/attachment_evidence.json --active-catalog --variants single_agent_dense multi_agent --output evals/reports/my_single_vs_multi.json
```
