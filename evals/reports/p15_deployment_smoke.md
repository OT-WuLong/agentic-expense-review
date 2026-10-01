# P15 本地容器验收记录

日期：2026-09-25。范围为本机 Docker Desktop 与项目合成数据，不是公网可用性测试。

| 检查 | 实测结果 |
| --- | --- |
| 多阶段构建 | Node 构建 Vue、Python 3.11 + 锁定依赖构建应用镜像；`docker compose up -d --build` 成功，镜像仅为本地 `agentic-approval:local` |
| 启动顺序 | PostgreSQL、Milvus、etcd、MinIO 健康；`bootstrap` 退出码 0，Alembic 完成，导入 3 个合成申请、25 条规则，Milvus 中制度切片 52 条；应用健康 |
| 同源路由 | `/health` 200、刷新 `/approvals/demo` 返回 Vue HTML、未知 `/api/v1/unknown` 返回 JSON 404 |
| 可选 HTTPS 与 SSE | Caddy 配置验证通过；`https://localhost/health` 200；使用本机 Caddy CA 验证证书后，经 HTTPS 的 SSE 返回 `text/event-stream` 与首个 `APPROVAL_CREATED` 事件 |
| A：日常通勤 | `P15-A-afc4e1fb` → `BUSINESS_REJECTED` |
| B：住宿多跳 | `P15-B-e39feca3` → `COMPLETED` |
| C：制度冲突与人工恢复 | `P15-C-f77f9169` → `HUMAN_PENDING`，财务复核员提交合成演示决定后 → `COMPLETED` |
| 镜像内容 | `/app/.env`、`/app/TODO.md`、`/app/evals`、`/app/docs` 不存在；`/app/frontend/dist/index.html` 存在 |
| 最终代码检查 | 后端 `pytest -q` 146 passed；前端 Vitest 9 passed；Playwright 4 passed；Ruff、前端 lint、类型检查／构建通过 |

排错过程亦保留口径：第一次 B 的失败单 `P15-B-fdf4a803` 在 Retrieval Agent 调模型时遇到 `SSLError`；增加有间隔的有限连接重试后，另一单 `P15-B-37f4f261` 完成检索但累计 23,482 token，超过当时 20,000 的硬预算，被 Guardrail 判为 `SYSTEM_ERROR`。当前容器显式配置 32,000 token 上限，旧 Checkpoint 的只读复核显示相同用量在 20,000 时触发 `TOKEN_BUDGET`、在 32,000 时不触发；B/C 之后的实际演示均完成。重试和预算上调不能保证外部模型零故障，硬预算仍生效。

三个案例是在**最终部署配置下分别运行**，不是并发压力测试。演示申请、审计记录和持久卷保留在本机；没有推送镜像、公开开放端口或使用真实企业资料。新上传文档仍待解析／入索引，不能拿黄金案例的预解析附件代表任意上传能力。
