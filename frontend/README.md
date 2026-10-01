# Vue 前端

前端提供审批队列、结构化申请、Agent 轨迹、证据/规则、人工复核和制度清单。

先在仓库根目录启动 FastAPI：

```powershell
uv run --env-file .env uvicorn app.main:app --reload
```

再在另一个终端启动 Vue：

```powershell
npm --prefix frontend ci
npm --prefix frontend run dev
```

打开 Vite 输出的本地地址后进入 `/approvals`。开发代理将 `/api` 转发到 `http://127.0.0.1:8000`；可用 `VITE_API_TARGET` 覆盖。A/B/C 演示路径见[根目录 README](../README.md#演示路径)。

质量检查：

```powershell
npm --prefix frontend run type-check
npm --prefix frontend run test:unit -- --run
npm --prefix frontend run lint
npm --prefix frontend run build
```
