# Agentic ablation (validation)

## 新增 40 条中高难案例：本轮结果

两组均使用 `deepseek-flash`、同一 Dense 检索器、单公司制度目录、131 条隔离结构化记录、预解析附件和确定性规则。每组完整运行一次，失败不剔除，未根据结果修改金标或重新挑选样本。原始结果在同名 `_raw` 目录；原有 60 条报告保持不变，**本轮不是一次重新运行全部 100 条的实验**。

| 指标 | 单轮单 Agent + Dense | 多 Agent 完整流程 + Dense | 差值 |
| --- | ---: | ---: | ---: |
| 建议匹配率 | 30/40（75.0%） | 38/40（95.0%） | +20.0 个百分点 |
| 业务完整成功：建议、预定义关键证据、规则和风险项全部匹配 | 12/40（30.0%） | 15/40（37.5%） | +7.5 个百分点 |
| 原合同严格任务成功：另含工具能力、覆盖与停因 | 10/40（25.0%） | 11/40（27.5%） | +2.5 个百分点 |
| 非通过样本中错误自动建议通过 | 0/25 | 0/25 | 0 |
| 转人工或补件比例 | 24/40（60.0%） | 18/40（45.0%） | -15.0 个百分点 |
| 系统错误 | 1/40 | 0/40 | -1 条 |

“建议匹配率”是通过、驳回、转人工三类建议与合成金标的一致率，不是系统给出通过建议的占比，也不等于真实企业合规准确率。

### 分组与实际收益

| 分组 | 单轮单 Agent 建议匹配 | 多 Agent 建议匹配 |
| --- | ---: | ---: |
| 中难 | 16/20（80.0%） | 20/20（100.0%） |
| 高难 | 14/20（70.0%） | 18/20（90.0%） |
| 住宿 | 10/16（62.5%） | 16/16（100.0%） |
| 餐饮 | 10/12（83.3%） | 10/12（83.3%） |
| 交通 | 10/12（83.3%） | 12/12（100.0%） |

8 条建议由不匹配变为匹配，没有反向退步：`H01/H02/H05/H12/H13/H16/T01/T09`。其中 H13 的基线失败是 `RetrievalPlan` 结构校验错误，不是网络超时；这条技术失败贡献了 2.5 个百分点，不能解释成业务推理提升。

可直接检查的闭环见证：

- **H01**：首轮已有发生日的常州 B 级记录，但未取到 B 级每间夜额度。Supervisor 引用 `R01-C01/R01-C02`，将目标改为定位住宿制度第七章的 B 级额度；`R02-C01` 补取标准后，建议由人工变为通过。
- **H05、H12**：补取 A 级每间夜标准后，得到明确超限的建议驳回，不再因首轮未取到额度而转人工。
- **T09**：首轮未取到提交时限条款；第二轮取得运营细则第三十五条的 45 天规定，确认第 43 天提交符合期限，建议由人工变为通过，且业务完整成功。
- **T01**：一轮即通过；基线则把尚待规则层核验的权限与查重当作补件缺口。这条是提示、职责边界和所选查询组合的差异，不能归因于补检。

多 Agent 的检索轮数为：一轮 27 条、两轮 12 条、三轮 1 条；改写次数相应为 0/1/2。没有发生正向挑战 Reviewer 的记录，所以本次不能声称证明了挑战机制的收益。业务完整成功的配对变化为 6 条改善、3 条退步，净增 3 条；不能只展示建议匹配的 8 条改善而忽略这个差别。

### 尚未解决的问题

**两组都误判 M02、M03。** M02 人均 100 元在旧版 120、新版 150 下都通过；M03 人均 170 元在两版都超限。模型仍因为标准数字不同而升级人工，尽管规则层分别已给出全部通过、全部超限的结果。Reviewer 的“标准有差异”与“本笔结论有实质分歧”边界需要修复，不应再搜索不存在的替代文件来拖延确定结论。

**完整证据评分明显落后于建议匹配，不能忽略。** 多 Agent 的住宿组建议 16/16 匹配，但业务完整成功仅 1/16，严格任务 0/16。缺失项中有两类问题：

1. 金标要求精确条款集合，没有充分列出等价证明来源。例如部门细则已有城市等级要求，却还固定要求通用住宿制度第二十一条；这个口径需要按制度审阅，不能用本轮预测反填。
2. 系统存在真实的引用完整性缺口。例如 H03、H16 的提交时限规则 `evidence_ids=[]`；H01 的 30 天时限虽然有引用，但引用的是总则第五条的“退房日期用于计算提交时限”，没有支撑 30 天这个具体值。规则参数计算正确，并不等于取得了支撑参数的制度原文。仅凭 Reviewer 对宽泛 Q-POLICY 的 `SUPPORTED` 不能保证每个必要规则的依据齐全。

本轮所有金标、分母及原始结果保持不变。若修正等价证据口径，应另发数据版本，并保留这次结果；若修复引用门或餐饮误升级，也应另存新运行，不能覆盖此报告。

### 代价与解释边界

记录到的 Agent LLM Token 为 480,785 → 722,791（约增加 50.3%）；非系统错误尝试的延迟 P50 为 41.6 → 51.7 秒。Token 不含失败模型调用和 Embedding，是下限；不同组固定先后各跑一次，延迟受服务波动影响，不能当独立速度结论。

这次可以说：**在 40 条自编中高难合成 validation 上，完整多 Agent 流程相对单轮 Agent + Dense 的建议匹配率提高 20 个百分点，收益主要集中在遗漏条款的定向补检。** 不能说：多 Agent 的独立贡献就是 20 个百分点。两组同时存在 Prompt、路由和检索轮数的差异；要隔离协作本身，仍需允许相同多轮、工具和预算的单 Agent 对照。

这些是开发可见、尚未经独立人员复核的合成案例，输入为预解析附件，不是原始 PDF 经 MinerU 的端到端测试，也不是外部盲测或真实企业票据准确率。

## 自动生成的原始汇总

- Status: `MEASURED`; commit: `7556c2b7bb92a8cd9dc040458c6a405231bf33e5` (dirty=True)
- Same synthetic samples and hard limits; one live run per variant.
- single_agent_dense uses one unified Agent prompt for planning and assessment, one retrieval round, and the same final rules. This compares the full workflow bundle, not agent count alone.
- Cost and exact duplicate-query rate are not measured.

| Variant | Business success | Strict success | Recommendation match | False auto-pass | System errors | Non-error P50 ms | Agent tokens* |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| single_agent_dense | 12/40 | 10/40 | 75.0% | 0.0% | 2.5% | 41593 | 480785 |
| multi_agent | 15/40 | 11/40 | 95.0% | 0.0% | 0.0% | 51689 | 722791 |

- Recommendation difference: +20.00 percentage points.
- Business success difference: +7.50 percentage points; paired wins/losses: 6/3.
- Business success requires correct suggestion, required final evidence, rule outcomes, risk flags and allowed tools; it excludes architecture-specific tool-call counts and stop labels.
- Synthetic pre-parsed attachment inputs; one run per variant, not a raw-PDF end-to-end or external generalisation score.

*Agent tokens are a lower bound; failed calls and embeddings are excluded.
