# GraphRAGAgent 线下面试 Demo

这个版本把现有 GraphRAGAgent 项目整理成一个可线下演示、可离线启动、可排障说明的交付包。它适合用来回应“智能体功能开发、调试、性能优化、一键部署、U 盘离线交付、产品落地测试”这类岗位要求。

## 你现场要展示什么

第一步，直接启动：

```bash
./scripts/start-demo.sh
```

脚本会启动两个服务：

- 后端 API：`http://127.0.0.1:8000/api/v1`
- 前端页面：`http://127.0.0.1:5173`

第二步，跑验收：

```bash
./scripts/verify-demo.sh
```

验收会检查 `/health`、`/system/stats`、`/system/demo` 和前端静态页面，证明系统不是只打开了一个空壳。

第三步，讲清楚能力边界：

- 断网可用：前端、后端、已有文档索引、知识图谱浏览、实体搜索、系统统计、健康检查。
- 可全离线新增索引：文本型 PDF、HTML、TXT、Markdown 可以走本地解析；实体抽取需要本地 OpenAI-compatible LLM。
- 需要 OCR 或云解析：扫描 PDF、图片、Office 文档建议走 MinerU 或后续接本地 OCR。
- 需要模型：智能问答和新增索引的实体抽取需要本地模型服务或云 API Key。
- 离线包不会包含真实 `backend/.env`，避免把密钥带到 U 盘里。

## 推荐面试话术

可以这样讲：

> 我把 GraphRAGAgent 拆成可交付的运行态。面试现场不用先装依赖，脚本会优先使用包里的后端虚拟环境和已经构建好的前端产物；没有真实模型 Key 时，系统仍然可以展示已有 KG 数据、搜索和健康检查。需要客户现场新增索引时，文本型 PDF/HTML/TXT/Markdown 可走本地 parser，实体抽取和 QA 可接本地 OpenAI-compatible 模型或客户内网模型服务。

如果面试官问“为什么健康检查显示 degraded”，回答：

> 这是刻意保留的真实状态。离线包里没有云模型 Key 和 MinerU Token，所以模型和云解析组件会显示未配置；但 document_parser/local、storage、KG、API、前端都能验收通过。这样比把假 Key 写进配置更适合交付。

## 新增索引怎么离线跑

`backend/.env.offline.example` 默认使用：

```env
PARSER_MODE=local
```

它支持：

- 文本型 PDF
- HTML
- TXT
- Markdown

要完成“上传文档 → 本地解析 → 实体抽取 → 生成 KG”的全离线链路，还需要把 `LLM_BASE_URL` 指向本机 OpenAI-compatible 服务，例如 Ollama、LM Studio、vLLM 或客户内网模型网关。

扫描件 PDF、图片、Office 文件不是默认主演示路径。现场可以说明它们需要 MinerU 云解析、本地 OCR 引擎，或提前索引后再打包。

## 一键打包

在项目根目录运行：

```bash
./scripts/package-offline-demo.sh
```

默认生成：

```text
offline-packages/GraphRAGAgent-offline-demo-时间戳.tar.gz
```

包内包含后端代码、前端 `dist`、已有 demo 数据、启动脚本、验收脚本、离线部署说明和排障文档。默认排除：

- `backend/.env`
- `.demo-runtime/`
- `frontend/node_modules/`
- Python/前端缓存文件

如果目标机器完全没有前端构建能力，一般也不需要带 `node_modules`，因为 demo 直接使用 `frontend/dist`。如果你确实要把前端依赖也带走，可以运行：

```bash
INCLUDE_NODE_MODULES=1 ./scripts/package-offline-demo.sh
```

## 面试实操建议

提前在本机跑一遍：

```bash
./scripts/stop-demo.sh
./scripts/start-demo.sh
./scripts/verify-demo.sh
```

然后准备三个问题给面试官看：

- “这个知识图谱里有哪些核心实体？”
- “某个实体的一跳邻居有哪些？”
- “系统健康状态里哪些组件离线可用，哪些需要模型配置？”
- “现场上传一份文本型 PDF 或 Markdown，演示本地 parser 如何生成新的 KG。”

这样你展示的是产品交付能力，不只是 AI demo。
