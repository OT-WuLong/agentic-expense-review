# 1.4 草案票据真实流程检查（2026-09-29）

本报告只覆盖六份新增合成票据，不是冻结 test，也不能外推为总体通过率。原始 Agent 结果保存在 [首单](./v14_invoice_live_meal01.json)和[其余五单](./v14_invoice_live_remaining.json)；它们是两次独立调用，不能说成一次六单运行。

## 真实模型 Agent 路径

使用 `deepseek-flash`、默认 Dense 制度检索、当前 PostgreSQL 规则目录和六份 PDF 提取出的附件切片；不是 Fake 模型。六单均 `COMPLETED / PASS_RECOMMENDED`，且报告中的严格 `task_success=true`：

| 样本 | Agent Step | 检索轮次 | 耗时 |
| --- | ---: | ---: | ---: |
| MEAL-01 | 5 | 2 | 85.4 s |
| MEAL-02 | 5 | 2 | 51.6 s |
| MEAL-03 | 3 | 1 | 133.9 s |
| TAXI-01 | 5 | 2 | 107.3 s |
| TAXI-02 | 3 | 1 | 57.4 s |
| TAXI-03 | 5 | 2 | 106.8 s |

这一层使用评测草案的 `POLICY-CATALOG-REF-1.2.0` 与预先从 PDF 提取的附件文本，未调用 MinerU 上传接口。

## 实际 PDF 上传路径

使用配置中的财务复核员身份，调用前端同源使用的 `POST /api/v1/approvals/with-invoice`，上传原始 PDF；服务端完成 MinerU 解析、Agent、规则路由和 PostgreSQL 持久化。两单的业务投影与 Checkpoint 均为 `CONSISTENT`。

| 申请 | MinerU 抽取 | 实际状态与建议 | 原因 |
| --- | --- | --- | --- |
| `REQ-V14-LIVE-MEAL-02-20260929` | `SYN-V14-MEAL-02`、300.00 元、2026-05-27 | `BUSINESS_REJECTED / REJECT_RECOMMENDED / HIGH` | 手工申请适用旧版通用餐饮制度：120 元/人 × 2 = 240 元，300 元超限。 |
| `REQ-V14-LIVE-MEAL-03-20260929` | `SYN-V14-MEAL-03`、240.00 元、2026-05-28 | `COMPLETED / PASS_RECOMMENDED / LOW` | 同一旧版额度下恰好 240 元，额度、票据一致性等规则均通过。 |

两单审计都有 `DOCUMENT_PARSED`、`APPROVAL_FINALIZED` 和 `CASE_WRITTEN`。没有伪造网页“申请人自行提交”：当前本地认证配置不提供市场部或运营部申请人令牌，因此这两次是财务复核员代合成员工提交的真实 HTTP 后端流程，未覆盖浏览器表单操作。

## 口径差异

草案 Agent 评测使用运营/市场部门参考细则，餐饮额度为 **160 元/人**；网页手工申请使用 `POLICY-CATALOG-SYN-1.1.0` 通用制度，2026-05-27 只适用旧版 **120 元/人**。因此 MEAL-02 在草案通过、实际上传驳回，二者各自遵守其制度快照，不能把差异归因于 MinerU 或 Agent 随机性。要让草案样本直接代表网页申请，必须先统一制度目录与规则快照，再重跑；本次没有擅自改动线上适用规则。
