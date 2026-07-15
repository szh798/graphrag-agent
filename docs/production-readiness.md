# 生产部署检查单

## 必需依赖

- `GRAPHRAG_APP_BACKEND=postgres` 与有效的 `DATABASE_URL`
- `GRAPHRAG_BLOB_BACKEND=vercel_blob` 与有效的 `BLOB_READ_WRITE_TOKEN`
- `GRAPHRAG_QUEUE_BACKEND=upstash` 与 Upstash REST URL/Token
- `GRAPHRAG_GRAPH_BACKEND=postgres` 使用 Neon 持久化图谱，或配置 Neo4j AuraDB
- 独立的索引 Worker 持续消费队列
- `PUBLIC_DOCUMENT_IDS` 只包含审核通过的公开演示文档

生产环境的 `/api/v1/health/ready` 必须返回 `ready`。临时文件系统和本地线程队列在开发模式可用，但在生产环境会明确返回降级状态。

## 200MB 上传

管理端通过 `/api/v1/documents/upload/direct` 获取短期上传令牌，浏览器将文件直接、多部分上传到私有 Vercel Blob；Blob 完成回调再将元数据登记到 FastAPI。文件字节不经过 Vercel Function 请求体。

## 发布顺序

1. 按 [生产运维手册](operations-runbook.md) 备份数据库与 Blob，验证校验和并在隔离环境做恢复演练，然后再应用 schema。
2. 验证对象存储和任务队列。
3. 发布后端预览并执行 health/smoke。
4. 发布 Sites 前端。
5. 验证公开白名单、问答、批任务和手机端导航。
6. 检查错误日志和请求 ID 关联。
