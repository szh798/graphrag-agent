# GraphRAGAgent 离线 Demo 排障手册

## 1. 启动失败

先看日志：

```bash
tail -n 120 .demo-runtime/logs/backend.log
tail -n 120 .demo-runtime/logs/frontend.log
```

再确认 Python：

```bash
backend/.venv/bin/python --version
backend/.venv/bin/python -c "import fastapi, uvicorn, langextract; print('ok')"
```

如果没有 `backend/.venv/bin/python`，说明包不是当前系统可直接运行的运行态包。处理方式：

- 同系统重新打包，确保带上 `backend/.venv`
- 或准备 `offline/wheels`，再让启动脚本离线安装
- 或在目标机器上安装 Python 3.12+ 和依赖

## 2. 端口被占用

默认端口是后端 `8000`，前端 `5173`。

macOS/Linux：

```bash
lsof -i :8000
lsof -i :5173
```

换端口启动：

```bash
BACKEND_PORT=8010 FRONTEND_PORT=5180 ./scripts/start-demo.sh
BACKEND_PORT=8010 FRONTEND_PORT=5180 ./scripts/verify-demo.sh
```

## 3. `/health` 是 degraded

纯离线演示里这是正常现象。判断顺序：

- `storage` 为 `ok`：本地数据可读
- `document_parser` 为 `ok` 且 `active_parser=local`：本地解析可用
- `langextract_venv` 为 `ok`：后端运行环境完整
- `llm_api` 为 `error`：没有配置模型 Key 或本地模型服务
- `mineru_api` 为 `error`：没有配置 MinerU Token

面试时可以直接解释：

> 离线包不会携带真实密钥，所以模型和云解析组件显示未配置；这不是启动失败，而是授权边界。已有 KG 浏览、搜索、本地文本解析仍然可用。

## 4. 前端能打开但接口报错

前端 API 当前指向：

```text
http://localhost:8000/api/v1
```

如果后端换了端口，前端静态构建仍然会访问 `8000`。面试现场建议优先释放 `8000` 端口。如果必须换端口，需要重新构建前端或把 `frontend/src/app/api.ts` 的 `BASE` 改成目标地址。

验证后端是否可用：

```bash
curl http://127.0.0.1:8000/api/v1/health
curl http://127.0.0.1:8000/api/v1/system/stats
```

## 5. 没有图谱数据

检查：

```bash
ls backend/data/kg
cat backend/data/docs_index.json
```

如果 `/api/v1/system/demo` 返回 `3002`，说明当前包里没有可展示的 KG。解决方式：

- 在有模型和解析服务的环境里提前索引一个 demo 文档
- 确认 `backend/data/kg/kg_nodes.json` 和 `backend/data/kg/kg_edges.json` 被打进包
- 重新运行 `./scripts/package-offline-demo.sh`

## 6. 智能问答失败

智能问答需要模型配置。检查：

```bash
cat backend/.env
curl http://127.0.0.1:8000/api/v1/health
```

常见原因：

- `LLM_API_KEY` 为空
- `LLM_BASE_URL` 指向了不可访问的地址
- 本地模型服务没有启动
- 模型不兼容 OpenAI chat completions 接口
- KG 数据为空

处理顺序：

1. 先保证 `/api/v1/system/demo` 有节点。
2. 再保证 `LLM_BASE_URL` 可访问。
3. 最后测试 QA。

## 7. 新文档索引失败

新增文档解析现在有两条路径：

- 全离线轻量路径：`PARSER_MODE=local`，上传文本型 PDF、HTML、TXT、Markdown。
- 半离线路径：配置 `MINERU_API_TOKEN`，让 MinerU 解析扫描件、图片或复杂版式。
- OCR 增强路径：后续接入客户现场 OCR 服务或本地 OCR 引擎。

如果 `PARSER_MODE=local` 仍失败，检查：

```bash
cat backend/.env
tail -n 120 .demo-runtime/logs/backend.log
```

常见原因：

- PDF 是扫描件，没有可选择文本
- 上传的是图片或 Office 文件
- 没有配置本地/云 LLM，解析后实体抽取失败
- 本地模型服务不兼容 OpenAI chat 接口

## 8. 面试现场的排障讲法

可以按这个顺序说：

1. 先看服务有没有启动：进程、端口、日志。
2. 再看依赖是否完整：Python、虚拟环境、关键包导入。
3. 再看配置是否真实：`.env`、模型地址、Token。
4. 再看数据是否存在：`backend/data/kg`、`docs_index.json`。
5. 再看 parser 模式：`local` 适合文本型文件，`mineru` 适合扫描件和复杂版式。
6. 最后区分产品问题和授权问题：离线包可展示已有能力，新增文本型索引需要本地模型，扫描件 OCR 需要 MinerU 或 OCR 服务。
