# GraphRAG Studio Production Architecture

## Runtime Split

- Vercel: frontend static build only.
- Railway/Fly.io: FastAPI API and long-running worker.
- Neo4j AuraDB: graph, chunks, embeddings, vector indexes, path queries.
- Neon Postgres: documents, jobs, sessions, batch QA, audit logs.
- Vercel Blob: uploaded files, MinerU artifacts, export files.
- Upstash Redis: indexing queue, locks, rate limits, short-lived cache.

LightRAG 双引擎使用**另一套** Neo4j Aura 图谱和独立 Neon 检索库；经典图谱、
业务数据库和 LightRAG 数据不得共库。API/Worker 资源、变量及回滚步骤见
[LightRAG 双引擎部署与运维手册](lightrag-dual-engine-operations.md)。

## Backend Environment

```bash
GRAPHRAG_GRAPH_BACKEND=neo4j
NEO4J_URI=neo4j+s://your-aura-instance.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=...
NEO4J_DATABASE=neo4j
NEO4J_VECTOR_DIMENSIONS=1024
LLM_EMBEDDING_MODEL=embedding-3
LLM_EMBEDDING_DIMENSIONS=1024
GRAPHRAG_ENABLE_EMBEDDINGS=auto
GRAPHRAG_ENABLE_VECTOR_RETRIEVAL=auto

GRAPHRAG_APP_BACKEND=postgres
DATABASE_URL=postgresql://...

GRAPHRAG_BLOB_BACKEND=vercel_blob
BLOB_READ_WRITE_TOKEN=...

GRAPHRAG_QUEUE_BACKEND=upstash
UPSTASH_REDIS_REST_URL=https://...
UPSTASH_REDIS_REST_TOKEN=...
INDEX_QUEUE_KEY=graphrag:index:queue
```

## Bootstrap

Run the schema setup and dry-run migration from the backend directory:

```bash
uv pip install -r requirements.txt
python -m scripts.bootstrap_production
python -m scripts.import_file_store_to_postgres
python -m scripts.import_file_store_to_postgres --apply
python -m scripts.import_file_store_to_neo4j
python -m scripts.import_file_store_to_neo4j --apply
```

The Postgres importer migrates JSON documents, jobs, sessions, query history,
and batch QA records into `AppRepository`. The Neo4j importer migrates the
existing local JSON demo graph into the configured graph repository.

## API and Worker

Run API:

```bash
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
```

Run worker:

```bash
python -m scripts.worker
```

Railway can use `backend/Dockerfile` plus `backend/railway.toml`. Fly.io can
copy `backend/fly.toml.example` to `backend/fly.toml` and set secrets.

## Readiness

Use `/api/v1/health/ready` before promoting a deployment. In production it
should report `ok` for:

- `graph_database`
- `app_database`
- `blob_storage`
- `task_queue`
- `mineru_api`
- `llm_api`

The Settings page shows the same dependency state in the UI.

## Frontend Environment

Because Vercel now serves only static frontend assets, set this Vercel
environment variable:

```bash
VITE_API_BASE_URL=https://your-railway-or-fly-backend.example.com/api/v1
```

## Stage Status

- Stage 1: production dependency hooks, Docker API runtime, and Vercel static frontend split are implemented. Real cloud resources still need credentials in deployment secrets.
- Stage 2: AppRepository supports filesystem fallback and Postgres CRUD for documents, jobs, sessions, query history, batch QA, and audit schema.
- Stage 3: uploads/artifacts go through BlobRepository; indexing can enqueue durable jobs through Upstash and a long-running worker can consume them.
- Stage 4: Neo4j stores entities/chunks/relations, creates vector/full-text indexes, writes embeddings during indexing, and uses hybrid retrieval for QA context.
