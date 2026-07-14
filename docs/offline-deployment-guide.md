# GraphRAGAgent 离线部署说明

## 目标

把 GraphRAGAgent 做成 U 盘可复制的线下面试交付包。目标不是在没有任何模型的情况下完成所有 AI 能力，而是保证客户现场或面试现场能稳定启动产品、展示已有知识图谱、验证服务状态，并清楚说明模型、解析服务、授权配置如何接入。

## 交付包结构

```text
GraphRAGAgent-offline-demo/
  backend/
    .env.offline.example
    .venv/
    data/
    requirements.txt
  frontend/
    dist/
  scripts/
    start-demo.sh
    stop-demo.sh
    verify-demo.sh
    package-offline-demo.sh
  docs/
    offline-deployment-guide.md
    troubleshooting-offline-demo.md
  README-interview-demo.md
  RUN-DEMO.txt
```

## 启动方式

进入解压后的目录：

```bash
./scripts/start-demo.sh
```

脚本会做以下事情：

- 优先选择 `backend/.venv/bin/python` 或 `backend/.venv/Scripts/python.exe`
- 如果没有 `backend/.env`，从 `backend/.env.offline.example` 创建一个离线配置
- 检查后端依赖是否可导入
- 启动 FastAPI 后端
- 用 Python 静态服务器托管 `frontend/dist`
- 等待后端和前端 URL 可访问

默认端口：

```text
Backend:  127.0.0.1:8000
Frontend: 127.0.0.1:5173
```

如果端口冲突：

```bash
BACKEND_PORT=8010 FRONTEND_PORT=5180 ./scripts/start-demo.sh
```

## 验收方式

```bash
./scripts/verify-demo.sh
```

验收脚本检查：

- `/api/v1/health`
- `/api/v1/system/stats`
- `/api/v1/system/demo`
- 前端静态页面

`/health` 可能显示 `degraded`，这在纯离线包里是正常的。因为没有真实 `LLM_API_KEY` 和 `MINERU_API_TOKEN`，模型问答和新文档云解析不会被标记为可用。只要 storage、API、前端、已有 KG demo 验收通过，就能完成线下面试展示。

## 模型与解析服务配置

半离线模式：

```text
前端、本地 API、已有 KG、搜索都在本机运行
LLM 和 MinerU 仍然走客户内网或公网 API
```

全离线模式：

```text
前端、本地 API、已有 KG、搜索、本地文档解析都在本机运行
LLM_BASE_URL 指向本机 OpenAI-compatible 服务
文本型 PDF/HTML/TXT/Markdown 走本地 parser
```

`backend/.env` 示例：

```bash
LLM_PROVIDER=local
LLM_API_KEY=local-key
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen2.5:7b
LLM_INDEX_MODEL=qwen2.5:7b
PARSER_MODE=local
MINERU_API_TOKEN=
```

如果只展示已有 KG，不需要配置真实 Key。若要现场新增索引，则必须配置一个可用 LLM，因为实体抽取仍然需要模型。

## Parser 模式

`backend/.env` 支持：

```env
PARSER_MODE=auto
```

取值说明：

- `auto`：有 `MINERU_API_TOKEN` 时用 MinerU；没有 Token 时用本地 parser。
- `local`：强制本地 parser，适合线下面试包。
- `mineru`：强制 MinerU 云解析。

本地 parser 支持：

- 文本型 PDF
- HTML
- TXT
- Markdown

本地 parser 不做 OCR。扫描件 PDF、图片、Office 文件需要 MinerU、客户已有 OCR 服务，或未来接入本地 OCR 引擎。

## 重新打包

```bash
./scripts/package-offline-demo.sh
```

可以把包输出到指定目录：

```bash
OUTPUT_DIR=/tmp/graphrag-demo ./scripts/package-offline-demo.sh
```

默认不会打包 `backend/.env`，这是为了避免泄露真实 API Key。现场如需启用云模型或本地模型，在目标机器上手动创建或编辑 `backend/.env`。

## 面试展示顺序

推荐顺序：

1. 解压 U 盘包。
2. 运行 `./scripts/start-demo.sh`。
3. 打开前端，展示文档、知识图谱、搜索页面。
4. 运行 `./scripts/verify-demo.sh`，展示自动化验收证据。
5. 打开 `/api/v1/health`，解释哪些组件离线可用，哪些组件需要授权。
6. 如现场有本地模型配置，上传一份文本型 PDF/TXT/Markdown，演示新增索引。
7. 如现场有模型配置，再演示 QA。

这样可以覆盖“开发、部署、授权、离线、排障、文档交付”的完整岗位要求。
