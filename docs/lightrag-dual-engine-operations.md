# LightRAG 双引擎部署与运维手册

本文只描述生产基础设施、发布、回填、评测和回滚。产品接口以现有
FastAPI 契约为准；浏览器不得直接调用 Railway 内部运行时。

## 1. 生产拓扑与边界

```text
Browser
  -> Vercel public gateway (Clerk, tenant checks, rate limits)
       -> timestamp + nonce + body SHA-256 + HMAC
          -> Railway LightRAG API: lightrag_integration.runtime:app
Railway indexing worker: python -m scripts.worker
  -> Upstash durable queue
  -> the same LightRAG Core/storage configuration

LightRAG Core -> dedicated Neon (KV, vectors, document status)
              -> dedicated Neo4j Aura (entities and relations)
```

Railway API 与 Worker 使用同一个
`deploy/lightrag/Dockerfile` 构建产物，但必须建立为两个独立服务以便单独扩缩容。
Railway 服务的 Root Directory 必须是仓库根目录，否则 Docker 无法复制
`backend/` 与 `deploy/lightrag/`。API 选择 `railway-api.toml`，Worker 选择
`railway-worker.toml`。

`/live` 是唯一匿名内部端点，只证明进程存活。`/internal/v1/*`（包括真正的
LightRAG health）全部要求 HMAC。运行时不提供 Swagger、WebUI 或 CORS；Railway
URL 虽需能被 Vercel 访问，但绝不能放入 `VITE_*` 或返回给浏览器。

## 2. 资源准备

1. 创建独立 Neon 项目/数据库，不复用账号、会话和审计数据库。在 Neon SQL
   Console 执行 `CREATE EXTENSION IF NOT EXISTS vector;`，确认实际恢复窗口并做一次
   隔离分支恢复演练。
2. 创建独立 Neo4j Aura 实例，不复用经典引擎图谱。记录 Aura 的备份策略和一次
   非生产恢复演练。
3. Railway 建立 API 与 Worker 两个服务，使用同一 Git commit 和 Dockerfile。
   API 最少一个实例；Worker 初始固定一个实例，避免绕过“每人一个索引任务”锁。
4. 为 API 与 Worker 分别挂载各自的 Railway 持久卷到 `LIGHTRAG_WORKING_DIR`（Railway
   卷不能跨服务共享）。权威数据在 Neon/Neo4j，但该目录仍含本地缓存与运行产物，
   重启时不应任意丢失。
5. Upstash 继续使用现有索引队列。LightRAG 不新建浏览器可写队列。

LightRAG 版本固定为 `lightrag-hku==1.5.4`。完整 Python 3.12 依赖及发行包哈希位于
`deploy/lightrag/requirements.lock`。升级时先修改 `requirements.in`，重新解析锁，
跑完双引擎评测后才能发布，禁止使用 `latest`。

镜像只安装 LightRAG Core；产品使用自己的 HMAC 内部接口，不安装也不暴露
LightRAG 自带 WebUI/API extra。`greenlet` 作为显式跨平台依赖锁定，避免在 macOS
生成的哈希锁遗漏 Railway Linux x86_64 的 SQLAlchemy 条件依赖。

## 3. 环境变量清单

真实值只进入 Vercel/Railway Secret Manager；`deploy/lightrag/env.example` 是无密钥
清单。`LIGHTRAG_HMAC_SECRET` 与 `LIGHTRAG_WORKSPACE_SECRET` 必须分别生成、至少
32 字节且不得复用。

### 3.1 Vercel 公共网关

| 变量 | 必需 | 值/用途 |
|---|---:|---|
| `LIGHTRAG_ENABLED` | 是 | 初次部署为 `false`，验收后改为 `true` |
| `LIGHTRAG_VERSION` | 是 | `1.5.4` |
| `LIGHTRAG_STRICT_VERSION` | 是 | `true`，版本不符时 fail closed |
| `LIGHTRAG_TRANSPORT` | 是 | `remote` |
| `LIGHTRAG_BASE_URL` | 是 | Railway API HTTPS origin，不含 `/internal/v1` |
| `LIGHTRAG_HMAC_SECRET` | 是 | gateway 与 Railway API 共享的签名密钥 |
| `LIGHTRAG_WORKSPACE_SECRET` | 是 | 将可信 `tenant_id` 映射为不可反推 workspace |
| `LIGHTRAG_HMAC_MAX_AGE_SECONDS` | 是 | `300`，允许范围 30–900 秒 |
| `LIGHTRAG_TIMEOUT_SECONDS` | 是 | `300`；允许范围 1–1800 秒 |
| `LIGHTRAG_DEFAULT_MODE` | 是 | `mix`，新版前端仍显式发送用户选择 |
| `LIGHTRAG_MAX_GRAPH_NODES` | 是 | 首屏默认 `200` |
| `LIGHTRAG_MAX_GRAPH_EDGES` | 是 | 布局边默认 `2000` |
| `LIGHTRAG_EXPORT_MAX_NODES` | 是 | 完整业务查询/JSON 导出的安全上限，默认 `10000` |
| `LIGHTRAG_EXPORT_MAX_EDGES` | 是 | 完整业务查询/JSON 导出的安全上限，默认 `100000` |

`LIGHTRAG_MAX_GRAPH_*` 只约束交互画布。节点/边分页、详情、邻居、路径、
图搜索和统计使用独立的 `LIGHTRAG_EXPORT_MAX_*` 完整快照，不能被首屏
`200/2000` 切片截断。完整快照仍受显式安全上限保护，避免无界内存和响应体。

Vercel **不得配置** `POSTGRES_*`、`NEO4J_*`、`UPSTASH_*` 的 LightRAG 凭据，也
不得直连检索数据库。

### 3.2 Railway API 与 Worker 共享

| 变量 | 必需 | 值/用途 |
|---|---:|---|
| `LIGHTRAG_ENABLED` | 是 | canary 前 `false`，正式双索引时 `true` |
| `LIGHTRAG_VERSION` / `LIGHTRAG_STRICT_VERSION` | 是 | `1.5.4` / `true` |
| `LIGHTRAG_TRANSPORT` | 是 | `local`；Railway 内嵌 LightRAG Core |
| `LIGHTRAG_WORKSPACE_SECRET` | 是 | 与 Vercel 相同；不要设置 `POSTGRES_WORKSPACE` 或 `NEO4J_WORKSPACE` |
| `LIGHTRAG_WORKING_DIR` | 是 | `/var/lib/lightrag/rag_storage` |
| `LIGHTRAG_WORKSPACE_CACHE_MAX` | 是 | 每个进程最多缓存 `64` 个 workspace Core 实例，超限按最近最少使用淘汰 |
| `LIGHTRAG_WORKSPACE_CACHE_TTL_SECONDS` | 是 | 空闲实例 `1800` 秒后可淘汰；执行中的 workspace 不会被关闭 |
| `LIGHTRAG_HEALTH_PROBE_TIMEOUT_SECONDS` | 是 | Neon、Neo4j、LLM、reranker 和 Upstash 单次探针最多 `5` 秒 |
| `LIGHTRAG_HEALTH_PROBE_TTL_SECONDS` | 是 | 真实探针结果缓存 `300` 秒，避免模型自检形成调用压力 |
| `LIGHTRAG_REQUIRE_DURABLE_QUEUE` | 是 | `true`；Railway/生产环境即使误设为 `false` 也拒绝本地队列 |
| `LIGHTRAG_KV_STORAGE` | 是 | `PGKVStorage` |
| `LIGHTRAG_VECTOR_STORAGE` | 是 | `PGVectorStorage` |
| `LIGHTRAG_DOC_STATUS_STORAGE` | 是 | `PGDocStatusStorage` |
| `LIGHTRAG_GRAPH_STORAGE` | 是 | `Neo4JStorage` |
| `LIGHTRAG_INDEX_MODEL` | 是 | 固定抽取模型 ID |
| `LIGHTRAG_SUMMARY_LANGUAGE` | 是 | `Chinese` |
| `LLM_PROVIDER` | 是 | 现有 OpenAI-compatible provider 标识 |
| `LLM_API_KEY` / `LLM_BASE_URL` | 是 | 现有 GLM 接口凭据和 `/v1` base URL |
| `LLM_MODEL` / `LLM_INDEX_MODEL` | 是 | 查询模型 / 抽取模型 |
| `LLM_EMBEDDING_MODEL` | 是 | 固定 embedding 模型；上线后不可原地更换 |
| `LLM_EMBEDDING_DIMENSIONS` | 是 | 与模型和既有向量表完全一致，如 `1024` |
| `RERANK_BINDING` | 是 | `cohere`（兼容接口） |
| `RERANK_MODEL` / `LIGHTRAG_RERANK_MODEL` | 是 | `BAAI/bge-reranker-v2-m3` |
| `RERANK_BINDING_HOST` / `RERANK_BINDING_API_KEY` | 是 | reranker endpoint 与密钥 |

专用 Neon：`POSTGRES_HOST`、`POSTGRES_PORT=5432`、`POSTGRES_USER`、
`POSTGRES_PASSWORD`、`POSTGRES_DATABASE`、`POSTGRES_SSL_MODE=require`、
`POSTGRES_MAX_CONNECTIONS=20`、`POSTGRES_VECTOR_INDEX_TYPE=HNSW`。

专用 Neo4j Aura：`NEO4J_URI=neo4j+s://...`、`NEO4J_USERNAME`、
`NEO4J_PASSWORD`、`NEO4J_DATABASE=neo4j`、
`NEO4J_MAX_CONNECTION_POOL_SIZE=50`。

Railway API 与 Worker 都设置 `GRAPHRAG_QUEUE_BACKEND=upstash` 及 Upstash
URL/Token，使签名保护的 readiness 能验证真实队列连通性；只有 Worker 会消费、确认
或恢复索引任务。探针会实际验证 pgvector 与事务写权限、Neo4j 事务写权限、查询/抽取
模型、embedding 维度、reranker 和 Upstash，并只返回状态、延迟和错误类型，不返回
主机名、密钥或异常正文。

### 3.3 Railway API 专用

- `LIGHTRAG_HMAC_SECRET`：与 Vercel 相同。
- `LIGHTRAG_HMAC_MAX_AGE_SECONDS=300`：Railway 和 Vercel 主机时钟必须同步。
- `LIGHTRAG_REQUIRE_DISTRIBUTED_NONCE=true`：生产必须启用；API 所有副本通过
  `LIGHTRAG_NONCE_REDIS_REST_URL/TOKEN` 共用 Upstash nonce fence，禁止仅使用进程内
  防重放后横向扩容。
- `PORT`：Railway 注入；默认 `8000`。
- API 启动命令：
  `uvicorn lightrag_integration.runtime:app --host 0.0.0.0 --port $PORT`。

每个签名包含 method、path、timestamp、nonce 和 body SHA-256；nonce 重放、过期请求、
正文变化或错误签名均返回 401。不要在日志中记录签名头、正文、问题或引用内容。

### 3.4 Railway Worker 专用

除共享变量外，Worker 还需要现有生产配置：

- `INDEX_QUEUE_KEY`、租约和 owner-lock 参数（Upstash 连接变量已列为 API/Worker 共享）。
- `GRAPHRAG_APP_BACKEND=postgres` 与经典业务库 `DATABASE_URL`。
- `GRAPHRAG_GRAPH_BACKEND=postgres`，确保经典图谱继续写入业务 Postgres；禁止在
  Worker 把它设为 `neo4j`，因为本部署中的 `NEO4J_*` 只属于 LightRAG Aura。
- `GRAPHRAG_BLOB_BACKEND=vercel_blob` 与 `BLOB_READ_WRITE_TOKEN`。
- 当前 MinerU、告警和索引租约变量保持不变。
- Worker 启动命令：`python -m scripts.worker`。

Worker 会在独立后台线程中向共享 Upstash 写入带 TTL 的心跳；即使单个索引任务运行很久，
心跳也会继续刷新。记录只包含不依赖 PID 的 Worker ID、应用版本和 `last_seen`。默认
`INDEX_WORKER_HEARTBEAT_TTL_SECONDS=120`、
`INDEX_WORKER_HEARTBEAT_INTERVAL_SECONDS=40`，且间隔会被安全限制在 TTL 的一半以内。
系统设置只展示 Worker ID 的哈希前缀。心跳缺失、格式错误、过期、队列不持久或 Worker
版本与网关不一致时，`/health/ready` 必须 fail-closed 返回 degraded；LightRAG API 自身的
依赖探针不能代替 Worker 存活证明。

Worker 的队列边界是同步的，但 `LIGHTRAG_TRANSPORT=local` 下的 LightRAG Core、
Postgres 与 Neo4j 客户端是异步且绑定 event loop。进程内所有同步调用统一提交到一个
daemon 线程维护的常驻 event loop，禁止在任务循环中重新引入 `asyncio.run()`；否则
第二个任务可能复用第一个已关闭 loop 上的连接或锁。bridge 会在 fork 后按新 PID
重建，并在进程退出时关闭。索引 Worker 应保持长进程运行，不要用每个任务 fork 一次
的执行模式。

LightRAG 删除 tombstone 只有在返回 `deleted=true` 且 `failed_page_ids` 为空时才能
标记 `done` 并确认队列 receipt。`deleted=false`、任何失败页或无效响应都会把 tombstone
恢复为 `queued`，等待 Upstash 租约恢复后重试；运维不得手工把部分删除记录改为完成。

`DATABASE_URL` 是经典业务库；LightRAG 的独立 Neon 必须只通过 `POSTGRES_*` 指向，
不要混用这两组连接配置。

旧文档可由 Worker 自动回填。首次发布在 Worker 设置
`LIGHTRAG_BACKFILL_ON_START=true`、`LIGHTRAG_BACKFILL_ALL_TENANTS_ACK=YES`，并用
`LIGHTRAG_BACKFILL_RELEASE_ID` 标识本次迁移。Worker 每批最多读取
`LIGHTRAG_BACKFILL_BATCH_SIZE` 份待补文档，将 LightRAG-only 子任务写入 Upstash，
并把脱敏 cursor/report 持久化到业务库。完成后应把开关恢复为 `false`；不要长期设置
`LIGHTRAG_BACKFILL_FORCE=true`。失败状态会在下一检查周期重试，已完成 release 不会重复入队。
系统设置中的 Backfill 卡片读取 `LIGHTRAG_BACKFILL_ON_START`、当前 release 对应的持久化
maintenance job、最后更新时间以及真实失败数，不再固定显示正常。任务异常退出会写入
`failed` 状态；`running` 超过两个检查周期未更新也会按 stale 处理。首次迁移完成并关闭
自动回填后，卡片会显示 `disabled`，同时保留最后一次维护状态供审计。

## 4. 发布流程

1. 按现有运维手册备份经典 Postgres、图谱和 Blob；另外对 LightRAG Neon/Aura 做
   发布前恢复点。确认两个环境的 embedding 模型及维度没有变化。
2. 在本地/CI 运行脚本测试、后端测试和生产构建。检查锁文件仍精确包含
   `lightrag-hku==1.5.4`。
3. 部署 Railway API，保持所有环境 `LIGHTRAG_ENABLED=false`。验证匿名 `/live`；
   内部 health 必须通过 Vercel gateway 触发，以验证 HMAC 链路。
4. 部署 Railway Worker，但先保持 worker `LIGHTRAG_ENABLED=false`，确认它只处理经典
   任务且队列没有重复消费。
5. API 与 Worker 改为 `LIGHTRAG_ENABLED=true`；Vercel仍为 `false`。上传一份脱敏
   canary 文档，直接从受控管理流程验证 LightRAG 子任务、Neon、Aura 和页码引用。
6. Vercel 改为 `LIGHTRAG_ENABLED=true` 并重新部署。验证经典引擎仍可用、LightRAG
   不可用时不会静默回退。
7. 先运行回填 dry-run，审核清单后最多应用一份 canary，再分批放量。观察错误率、
   LLM 调用、队列深度、数据库连接、引用页码和告警。

构建命令（仓库根目录）：

```bash
docker build \
  --file deploy/lightrag/Dockerfile \
  --build-arg LIGHTRAG_VERSION=1.5.4 \
  --tag graphrag-lightrag:1.5.4 .
```

## 5. 回填与评测

回填脚本默认只执行只读列表请求并打印计划，绝不排队：

```bash
cd backend
GRAPHRAG_GATEWAY_URL='https://admin.example.com/api/v1' \
LIGHTRAG_OPS_AUTH_TOKEN='set-in-shell-only' \
python -m scripts.lightrag_backfill
```

审核后才允许显式写入；先用 `--max-documents 1` canary：

```bash
python -m scripts.lightrag_backfill --apply --max-documents 1
```

默认接口可以通过环境变量调整：

- `LIGHTRAG_BACKFILL_DOCUMENTS_PATH=/documents`
- `LIGHTRAG_BACKFILL_RETRY_PATH_TEMPLATE=/indexing/{doc_id}/retry`
- `LIGHTRAG_OPS_AUTH_HEADER=Authorization`
- `LIGHTRAG_OPS_AUTH_TOKEN`（只在 shell/secret manager 设置）
- `LIGHTRAG_OPS_TIMEOUT_SECONDS=30`

失败过的 LightRAG 索引不会被普通 backfill 自动重试；人工确认原因后使用
`--include-failed --apply`。脚本只发送 `{"engine":"lightrag"}`，不会重跑或删除
经典索引。

上面的网关脚本只处理该运维账号可见的空间。正式全量回填必须在 Railway Worker
容器内执行可信的全租户脚本；它直接遍历业务库，但报告只输出租户哈希前缀：

```bash
cd /app/backend
python -m scripts.lightrag_backfill_worker --max-documents 0

LIGHTRAG_BACKFILL_ALL_TENANTS_ACK=YES \
python -m scripts.lightrag_backfill_worker --apply --max-documents 1
```

`--apply` 会拒绝本地线程队列，只允许通过现有 Upstash 持久队列入队；因此每个租户
仍由 Worker 的分布式锁保持最多一个父索引任务并发。审核 canary 后再移除
`--max-documents 1`，失败项使用 `--include-failed` 单独处理。

双引擎评测默认也只验证数据集并打印计划。正式数据集至少50题、10份脱敏文档；
答案产物包含敏感上下文，写到仓库外的加密目录：

```bash
python -m scripts.evaluate_dual_engine \
  ../evaluations/dual-engine/questions.jsonl \
  --run --modes all \
  --output /secure/evaluations/dual-engine.jsonl
```

## 6. Smoke 与验收

本地静态检查：

```bash
python -m unittest \
  tests.test_lightrag_backfill_script \
  tests.test_dual_engine_evaluation_script
bash -n ../scripts/smoke-lightrag.sh
python -m compileall -q lightrag_integration scripts
```

部署后，以管理账号 token 从 shell 执行：

```bash
GRAPHRAG_GATEWAY_URL='https://admin.example.com/api/v1' \
LIGHTRAG_BASE_URL='https://replace.up.railway.app' \
LIGHTRAG_OPS_AUTH_TOKEN='set-in-shell-only' \
../scripts/smoke-lightrag.sh
```

随后人工/Playwright 验收：

1. 上传一次后出现 legacy 与 lightrag 两个子状态，且用户层仍只有一个并发索引任务。
2. `local/global/hybrid/mix/naive` 五种模式均按用户选择返回，不自动改模式。
3. 引用跳转到正确文档与页码；LightRAG 图谱节点/边带引擎前缀且可拖动、重排和导出。
4. 匿名、个人、组织、公共 workspace 交叉查询均为零泄漏。
5. 删除同一文档后，两套索引均删除；重试不会生成重复页 ID。
6. 停掉 Railway API 时，前端明确提示切换经典引擎，不自动回退；恢复后重试成功。

## 7. 快速回滚

首选回滚不删除任何数据：

1. 将 Vercel `LIGHTRAG_ENABLED=false` 并重新部署；确认新会话和旧链接仍可使用经典
   引擎。此步骤立即隔离用户流量。
2. 将 Railway Worker `LIGHTRAG_ENABLED=false`，等待当前领取任务结束或租约超时；
   不清空 Upstash 队列、不删除失败任务。
3. 如故障来自代码，Railway API/Worker 同时回滚到上一个相同镜像 digest，禁止两者
   使用不同 LightRAG 版本。
4. 保留专用 Neon、Aura 和 workspace 数据供复盘。只有在确认备份可恢复且无待处理
   删除任务后，才能人工清理。
5. 用 `/api/v1/health/ready`、经典文档列表、经典图谱和经典问答完成回滚 smoke，检查
   request ID、飞书告警和队列深度。

如果 embedding 模型或维度配置错误，不要在原表上继续写入；先关闭 LightRAG、恢复
正确配置或恢复数据库，再重新索引。回滚经典引擎不需要回滚上传文件或账号数据。

## 8. 版本参考

- [LightRAG 1.5.4 PyPI 发行包](https://pypi.org/project/lightrag-hku/1.5.4/)
- [LightRAG 1.5.4 API Server 文档](https://github.com/HKUDS/LightRAG/blob/v1.5.4/docs/LightRAG-API-Server.md)
- [LightRAG 1.5.4 官方环境变量](https://github.com/HKUDS/LightRAG/blob/v1.5.4/env.example)
- [Neon pgvector 文档](https://neon.com/docs/extensions/pgvector)
