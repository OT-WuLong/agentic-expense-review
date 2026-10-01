# Agentic RAG 费用预审系统

一个面向**单企业演示场景**的费用报销预审项目。申请人上传发票并填写申请后，系统解析票据、检索适用制度，由 LangGraph Agent 核验证据，再结合确定性规则给出**建议通过、建议驳回或转人工**。它提供预审建议，不代替财务人员作最终审批。

> 所有随仓库提供的企业、员工、制度、票据和审批案例均为合成数据；公开制度只用于规则结构参考，不包含真实企业报销数据。当前是本地单机 MVP，不应直接用于生产审批。

## 项目亮点

- **证据驱动的多 Agent 流程：** Supervisor 决定检索或终止，Retrieval Agent 按问题选择制度与结构化查询工具，Evidence Reviewer 检查证据覆盖、冲突和缺口。模型不能绕过工具权限、预算上限或确定性规则。
- **可追溯的制度检索：** Milvus 按部门、费用类型、有效期、发布状态和目录快照过滤制度；实现 Dense、Qwen Sparse、BM25、RRF 与 Qwen Reranker。当前默认 Dense，混合检索可通过配置启用，不把小样本消融结果夸大为质量提升。
- **完整业务闭环：** MinerU 解析 PDF/图片票据；规则核对金额、日期、票据事实及制度边界；结果附制度版本、章节、页码和票据来源。材料缺失或制度冲突进入人工复核。
- **可观察、可恢复：** Vue 工作台展示 Agent 轨迹、证据和规则结果；PostgreSQL 保存审批、审计与 LangGraph Checkpoint，支持人工中断后恢复、幂等重试和 SSE 实时更新。

## 技术栈与流程

| 层 | 主要技术 |
| --- | --- |
| 前端 | Vue 3、TypeScript、Element Plus、Pinia、SSE |
| API / 工作流 | FastAPI、Pydantic、LangGraph |
| 文档 / 检索 | MinerU、Qwen Embedding / Reranker、Milvus |
| 存储 / 部署 | PostgreSQL、Docker Compose |

```text
申请 + 发票 ──→ MinerU 解析 ──→ LangGraph
                              ├─ Supervisor：决定目标和下一步
                              ├─ Retrieval Agent：调用制度/结构化工具
                              └─ Evidence Reviewer：核对证据与缺口
                                           ↓
                           确定性规则 + 风险路由
                                           ↓
                         预审建议 / 人工复核 / 审计
```

## 本地运行

需要 Docker Desktop，以及用于生成演示登录令牌的 Python 3.11 + [uv](https://docs.astral.sh/uv/)；前端单独开发时再安装 Node.js 22。首次启动会拉取 PostgreSQL、Milvus 等容器，并调用模型服务建立制度索引。

1. 复制 `.env.example` 为 `.env`。填写 `MINERU_TOKEN`、`DASHSCOPE_API_KEY`、以 `/api/v1` 或 `/compatible-mode/v1` 结尾的 `DASHSCOPE_BASE_URL`，以及所选 Agent 模型的密钥（默认 `DEEPSEEK_API_KEY`）。
2. 在**自己的终端**运行 `uv run python scripts/generate_auth_config.py`，把输出的 `APP_SESSION_SECRET` 和 `APP_AUTH_USERS_JSON` 填入 `.env`。不要提交或分享这些值。
3. 启动服务：

```powershell
docker compose up -d --build
docker compose ps
```

打开 <http://127.0.0.1:8000/login>。用 `uv run --env-file .env python scripts/generate_auth_config.py --show` 在本机查看演示角色令牌。`/health` 仅表示进程存活，`/ready` 同时检查 PostgreSQL 与必需的 Milvus 制度集合。

Compose 默认只将应用、数据库和 Milvus 端口绑定到 `127.0.0.1`。`.env.example` 中的数据库密码仅用于本地演示；公网部署前必须更换密码与令牌、启用 HTTPS 和安全 Cookie，并重新评估权限及数据合规。不要上传真实或未经授权的发票：解析会把文件发送到 MinerU 云端。

## 演示路径

登录后可在“新建审批”直接运行三个合成黄金案例：

| 案例 | 主要检查点 | 预期结果 |
| --- | --- | --- |
| `GC-A-TRANSPORT-COMMUTE-REJECT` | 日常通勤禁报条款 | 建议驳回 |
| `GC-B-LODGING-MULTIHOP-PASS` | 住宿制度 + 城市等级 | 建议通过 |
| `GC-C-DINING-VERSION-CONFLICT-HUMAN` | 同时有效的餐饮制度冲突 | 转人工复核 |

也可以用“手工录入”上传仓库中的合成 PDF，例如 `evals/datasets/draft_1_5/attachments/DOC-V15-T01.pdf`，填写交通费 86 元、发生日 `2026-06-11`、提交日 `2026-06-12`，事由“培训设备搬运协调：研发园区至客户培训中心”，观察票据解析、Agent 检索、证据核验和建议通过的全过程。演示数据中的员工、预算及发票查重结果是固定快照，不是实时企业财务系统。

## 评测与复现

在 [60 条合成 validation 的单 Agent / 多 Agent 对照](evals/reports/single_vs_multi_dense_1_5_2_deepseek_20261002_interpretation.md)中，两组均使用 `deepseek-flash`、Dense 检索、同一制度目录和确定性规则，各完整运行一次：

| 指标 | 单轮单 Agent + Dense | 多 Agent + Dense |
| --- | ---: | ---: |
| 预审建议匹配 | 57/60（95.0%） | 59/60（98.3%） |
| 业务完整成功：建议、关键证据、规则和风险项均正确 | 54/60（90.0%） | 56/60（93.3%） |
| 原合同严格任务成功：额外检查工具能力与 Agent 停因 | 47/60（78.3%） | 50/60（83.3%） |
| 非通过样本中错误自动建议通过 | 0/37 | 0/37 |
| 系统错误 | 1/60 | 0/60 |

本轮建议匹配和业务完整成功均高 3.3 个百分点。基线有一条模型超时，另有一条餐饮地点要求与金标边界待复核；均保留原分母和评分。多 Agent 的 60 条全部一轮结束，因此这次对照不能证明补检循环或挑战机制的收益。此前 [60/60、51/60 单独运行](evals/reports/validation_1_5_2_active_deepseek_postfix_full.md)仍保留，不与本轮基线混用。

这 60 条输入是**预解析的合成票据切片**，不是 60 次网页 PDF 上传，也不是独立外部盲测。另用原始合成 PDF 经上传接口、MinerU 和后台 Agent 跑通通过、驳回、转人工三条路径；网页手工上传流程也已验证一条通过案例。不要把这些结果外推为真实企业票据准确率或生产可用率。评测定义见 [评测契约](docs/evaluation-contract.md)，业务范围见 [范围约定](docs/scope.md)，语料来源见 [来源清单](data/fixtures/source_manifest.json)。

本地代码检查：

```powershell
uv run python -m pytest -q
uv run ruff check app tests scripts
npm --prefix frontend ci
npm --prefix frontend run build
```

## 仓库结构

```text
app/              API、Agent、检索、规则与持久化
frontend/         审批工作台
data/             合成制度、票据与结构化数据
evals/            合成评测集与逐例报告
docs/             冻结的业务范围与评测契约
scripts/          入库、评测和本地配置脚本
migrations/       数据库迁移
tests/            自动化检查
```

本项目尚未接入真实企业身份、预算、发票查重或多租户系统；新上传制度也不会自动发布并入索引。正式落地需要补齐这些权威数据源、真实票据抽取评测与部署安全措施。
