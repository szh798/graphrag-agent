# GraphRAG Studio

GraphRAG Studio 是一个面向文档知识图谱的公开演示与管理工作台，包含文档解析、实体关系抽取、图谱浏览、搜索、单题问答和批量问答。

## 目录

- `frontend/`：React + Vite + Cloudflare Worker/Sites 前端与公开访问边界
- `backend/`：FastAPI、索引流水线、图谱与问答服务
- `api/`：Vercel Functions 入口以及大文件直传令牌端点
- `docs/`：产品、架构和运维文档

## 本地开发

后端使用 `uv` 管理独立环境：

```bash
cd backend
uv venv
uv pip install -r requirements.txt
uv run uvicorn main:app --reload
```

前端：

```bash
cd frontend
npm ci --legacy-peer-deps
npm run dev
```

## 验证

```bash
npm test
npm --prefix frontend run typecheck
npm run build
backend/.venv/bin/python -m pytest backend/tests -q
```

生产部署要求持久化业务数据库、对象存储、持久任务队列和图谱数据库全部通过 `/api/v1/health/ready`。详细步骤见 [生产部署检查单](docs/production-readiness.md)。
