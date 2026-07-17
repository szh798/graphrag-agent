# GraphRAG Studio 生产运维手册

## 1. 日常状态

- `/api/v1/health/live`：进程是否存活。
- `/api/v1/health/ready`：数据库、对象存储、图谱与持久队列是否可用。
- 管理员登录后的“账户与组织 → 运维概览”：错误聚合、正式认证、告警、数据库恢复窗口和索引恢复调度状态。

不要把 `/health/live` 当成完整的生产验收；发布门禁使用 `/health/ready`，业务验收还必须覆盖登录、上传、索引、图谱与问答。

## 2. 索引任务恢复

Upstash 队列使用领取租约。任务完成后确认删除；函数被强制终止时，租约到期后由 `.github/workflows/index-recovery.yml` 重新入队。默认租约 330 秒，最多自动恢复 2 次，耗尽后文档会进入“失败”状态并写入运维事件。

Vercel 与 GitHub Actions 必须配置同一个 `INDEX_DISPATCH_SECRET`。该密钥只允许调用内部调度入口，不要暴露给浏览器。

## 3. 异常告警

所有后端异常会以 request ID 聚合到 `ops_events`。设置 `OPS_ALERT_WEBHOOK_URL` 后，错误级事件还会投递到外部告警系统。Webhook 接收端应验证来源、限流并避免记录请求正文或凭据。

飞书自定义机器人地址会被自动识别，并以包含 `GraphRAG` 关键词的文本消息发送；生产环境保持 `OPS_ALERT_WEBHOOK_PROVIDER=auto` 即可。飞书机器人应启用关键词校验并设置关键词 `GraphRAG`。通用 Webhook 接收端可将 provider 显式设为 `generic`，此时收到的 JSON 只包含事件类型、来源、request ID、租户/操作者标识、脱敏说明和安全上下文，不包含问题、回答、Cookie 或凭据。

Webhook 地址等同于发送凭据，只能配置在 Vercel Production 环境变量中，不得写入 `.env.example`、日志或 Git。修改生产环境变量后必须重新部署，并用一条可识别的测试告警确认飞书响应体中的业务状态为成功；HTTP 200 本身不足以证明投递成功。

## 4. 数据备份

Neon 的即时恢复窗口必须在控制台的 **Backup & Restore / Restore window** 中确认；不同套餐可用窗口不同，不能仅凭数据库连接成功推断已开启。核对后设置：

```env
DATABASE_PITR_ENABLED=true
DATABASE_BACKUP_RETENTION_HOURS=<已核实小时数>
```

另做独立导出，覆盖 Postgres 数据以及数据库中引用的 Vercel Blob 文件：

```bash
cd backend
DATABASE_URL='postgresql://...' \
BLOB_READ_WRITE_TOKEN='...' \
GRAPHRAG_APP_BACKEND=postgres \
GRAPHRAG_BLOB_BACKEND=vercel_blob \
./scripts/backup-production.sh /secure/offsite/backups

./scripts/verify-production-backup.sh /secure/offsite/backups/graphrag-<timestamp>
```

备份目录包含用户上传内容，必须加密并存放在与生产项目不同的账户或存储域中。脚本只输出目录和条目数量，不输出连接串或 Blob 凭据。

## 5. 恢复演练

每月至少在隔离的 Neon 分支或空白 Postgres 实例执行一次：

1. 运行 `verify-production-backup.sh` 校验数据库目录和所有 Blob 的 SHA-256。
2. 使用 `pg_restore --no-owner --no-privileges --dbname "$RESTORE_DATABASE_URL" database.dump` 恢复到隔离数据库。
3. 将备份的 Blob 还原到隔离 Blob Store，并依据 `blob-manifest.json` 核对条目数量和哈希。
4. 用隔离环境运行 `/health/ready`，抽查登录、文档列表、图谱和问答。
5. 记录演练时间、恢复点、耗时和失败项。不要把演练目标指向生产数据库。

Neon 支持在配置的历史保留窗口内执行时间点恢复；独立导出仍然必要，因为它同时覆盖对象存储误删、账户级故障和跨供应商恢复。
