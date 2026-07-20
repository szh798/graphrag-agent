"""Application data repository backends.

Postgres is the production home for transactional state: documents, indexing
jobs, chat sessions, batch QA records, and audit logs. The filesystem backend
remains the local demo fallback used by the current services.
"""
from __future__ import annotations

import os
from typing import Any

from storage import file_store as fs


LEGACY_OWNER_ID = "default"


def _owner_id(payload: dict) -> str:
    """Return the persisted owner, including for pre-isolation local data."""
    return str(payload.get("owner_id") or LEGACY_OWNER_ID)


class FileAppRepository:
    def profile(self) -> dict:
        return {"backend": "filesystem", **fs.storage_profile()}

    def health(self) -> dict:
        return {
            "status": "ok",
            "backend": "filesystem",
            "documents": len(fs.load_docs_index()),
            "jobs": len(fs.list_all_jobs()),
            "chat_sessions": len(fs.load_chat_sessions()),
            "batches": len(fs.list_batch_metas()),
            **fs.storage_profile(),
        }

    def save_document(self, doc: dict) -> None:
        fs.save_doc(doc)

    def get_document(self, doc_id: str) -> dict | None:
        return fs.get_doc(doc_id)

    def load_documents_index(self) -> dict[str, dict]:
        return fs.load_docs_index()

    def list_documents(self) -> list[dict]:
        return list(fs.load_docs_index().values())

    def find_document_by_blob(self, owner_id: str, blob_key: str) -> dict | None:
        return next((
            doc for doc in self.list_documents()
            if _owner_id(doc) == owner_id and str(doc.get("blob_key") or "") == blob_key
        ), None)

    def delete_document(self, doc_id: str) -> bool:
        index = fs.load_docs_index()
        if doc_id not in index:
            return False
        index.pop(doc_id, None)
        fs.save_docs_index(index)
        return True

    def update_document_status(self, doc_id: str, status: str, pages: int | None = None) -> None:
        index = fs.load_docs_index()
        if doc_id in index:
            index[doc_id]["status"] = status
            if pages is not None:
                index[doc_id]["pages"] = pages
            fs.save_docs_index(index)

    def save_job_meta(self, job_id: str, meta: dict) -> None:
        fs.save_job_meta(job_id, meta)

    def load_job_meta(self, job_id: str) -> dict | None:
        return fs.load_job_meta(job_id)

    def list_all_jobs(self) -> list[dict]:
        return fs.list_all_jobs()

    def delete_job(self, job_id: str) -> None:
        fs.delete_job(job_id)

    def save_chat_session(self, session: dict) -> None:
        payload = dict(session)
        payload["owner_id"] = _owner_id(payload)
        existing = fs.get_chat_session(payload["id"])
        if existing and _owner_id(existing) != payload["owner_id"]:
            return
        fs.save_chat_session(payload)

    def get_chat_session(self, session_id: str, owner_id: str = LEGACY_OWNER_ID) -> dict | None:
        session = fs.get_chat_session(session_id)
        if not session or _owner_id(session) != owner_id:
            return None
        return session

    def list_chat_sessions(self, owner_id: str = LEGACY_OWNER_ID) -> list[dict]:
        return [session for session in fs.list_chat_sessions() if _owner_id(session) == owner_id]

    def append_query_history(self, record: dict) -> None:
        payload = dict(record)
        payload["owner_id"] = _owner_id(payload)
        fs.append_query_history(payload)

    def load_query_history(self, owner_id: str = LEGACY_OWNER_ID) -> list[dict]:
        return [record for record in fs.load_query_history() if _owner_id(record) == owner_id]

    def save_batch_meta(self, batch_id: str, meta: dict) -> None:
        payload = dict(meta)
        payload["owner_id"] = _owner_id(payload)
        existing = fs.load_batch_meta(batch_id)
        if existing and _owner_id(existing) != payload["owner_id"]:
            return
        fs.save_batch_meta(batch_id, payload)

    def load_batch_meta(self, batch_id: str, owner_id: str = LEGACY_OWNER_ID) -> dict | None:
        meta = fs.load_batch_meta(batch_id)
        if not meta or _owner_id(meta) != owner_id:
            return None
        return meta

    def list_batch_metas(self, owner_id: str = LEGACY_OWNER_ID) -> list[dict]:
        return [meta for meta in fs.list_batch_metas() if _owner_id(meta) == owner_id]


class PostgresAppRepository:
    def __init__(self):
        self.database_url = os.getenv("DATABASE_URL", "").strip()

    def profile(self) -> dict:
        return {
            "backend": "postgres",
            "url_configured": bool(self.database_url),
        }

    def _connect(self):
        if not self.database_url:
            raise ValueError("DATABASE_URL is required when GRAPHRAG_APP_BACKEND=postgres")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Install psycopg[binary]>=3.2.0 to use GRAPHRAG_APP_BACKEND=postgres") from exc
        return psycopg.connect(self.database_url, connect_timeout=5, row_factory=dict_row)

    def _jsonb(self, data: dict | list) -> Any:
        try:
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError("Install psycopg[binary]>=3.2.0 to use GRAPHRAG_APP_BACKEND=postgres") from exc
        return Jsonb(data)

    def _fetch_payload(self, query: str, params: tuple = ()) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
        return dict(row["payload"]) if row and row.get("payload") else None

    def _fetch_payloads(self, query: str, params: tuple = ()) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        return [dict(row["payload"]) for row in rows if row.get("payload")]

    def health(self) -> dict:
        if not self.database_url:
            return {"status": "error", **self.profile(), "error": "DATABASE_URL is not configured"}
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            return {"status": "ok", **self.profile()}
        except Exception as exc:
            return {"status": "error", **self.profile(), "error": str(exc)}

    def ensure_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS app_documents (
              doc_id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL DEFAULT 'default',
              filename TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'uploaded',
              uploaded_at TIMESTAMPTZ,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS app_documents_owner_status_idx ON app_documents(owner_id, status)",
            """
            CREATE TABLE IF NOT EXISTS indexing_jobs (
              job_id TEXT PRIMARY KEY,
              doc_id TEXT NOT NULL,
              status TEXT NOT NULL,
              stage TEXT NOT NULL DEFAULT '',
              progress JSONB NOT NULL DEFAULT '{}'::jsonb,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS indexing_jobs_doc_status_idx ON indexing_jobs(doc_id, status)",
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
              session_id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL DEFAULT 'default',
              title TEXT NOT NULL DEFAULT '',
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS chat_sessions_owner_updated_idx ON chat_sessions(owner_id, updated_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS batch_qa_jobs (
              batch_id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL DEFAULT 'default',
              status TEXT NOT NULL DEFAULT 'submitted',
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS batch_qa_jobs_owner_updated_idx ON batch_qa_jobs(owner_id, updated_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
              audit_id BIGSERIAL PRIMARY KEY,
              owner_id TEXT NOT NULL DEFAULT 'default',
              event_type TEXT NOT NULL,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS query_history (
              query_id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL DEFAULT 'default',
              session_id TEXT,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS query_history_owner_created_idx ON query_history(owner_id, created_at DESC)",
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
            conn.commit()

    def save_document(self, doc: dict) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_documents (doc_id, owner_id, filename, status, uploaded_at, payload, updated_at)
                    VALUES (%s, %s, %s, %s, NULLIF(%s, '')::timestamptz, %s, now())
                    ON CONFLICT (doc_id) DO UPDATE SET
                      owner_id = EXCLUDED.owner_id,
                      filename = EXCLUDED.filename,
                      status = EXCLUDED.status,
                      uploaded_at = EXCLUDED.uploaded_at,
                      payload = EXCLUDED.payload,
                      updated_at = now()
                    """,
                    (
                        doc["doc_id"],
                        doc.get("owner_id", "default"),
                        doc.get("filename", ""),
                        doc.get("status", "uploaded"),
                        doc.get("uploaded_at", ""),
                        self._jsonb(doc),
                    ),
                )
            conn.commit()

    def get_document(self, doc_id: str) -> dict | None:
        return self._fetch_payload("SELECT payload FROM app_documents WHERE doc_id = %s", (doc_id,))

    def load_documents_index(self) -> dict[str, dict]:
        docs = self.list_documents()
        return {doc["doc_id"]: doc for doc in docs if doc.get("doc_id")}

    def list_documents(self) -> list[dict]:
        return self._fetch_payloads("SELECT payload FROM app_documents ORDER BY uploaded_at DESC NULLS LAST, created_at DESC")

    def find_document_by_blob(self, owner_id: str, blob_key: str) -> dict | None:
        return self._fetch_payload(
            """
            SELECT payload
            FROM app_documents
            WHERE owner_id = %s AND payload->>'blob_key' = %s
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (owner_id, blob_key),
        )

    def delete_document(self, doc_id: str) -> bool:
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app_documents WHERE doc_id = %s", (doc_id,))
                deleted = cur.rowcount > 0
            conn.commit()
        return deleted

    def update_document_status(self, doc_id: str, status: str, pages: int | None = None) -> None:
        doc = self.get_document(doc_id)
        if not doc:
            return
        doc["status"] = status
        if pages is not None:
            doc["pages"] = pages
        self.save_document(doc)

    def save_job_meta(self, job_id: str, meta: dict) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO indexing_jobs (job_id, doc_id, status, stage, progress, payload, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (job_id) DO UPDATE SET
                      doc_id = EXCLUDED.doc_id,
                      status = EXCLUDED.status,
                      stage = EXCLUDED.stage,
                      progress = EXCLUDED.progress,
                      payload = EXCLUDED.payload,
                      updated_at = now()
                    """,
                    (
                        job_id,
                        meta.get("doc_id", ""),
                        meta.get("status", ""),
                        meta.get("stage", ""),
                        self._jsonb(meta.get("progress", {})),
                        self._jsonb(meta),
                    ),
                )
            conn.commit()

    def load_job_meta(self, job_id: str) -> dict | None:
        return self._fetch_payload("SELECT payload FROM indexing_jobs WHERE job_id = %s", (job_id,))

    def list_all_jobs(self) -> list[dict]:
        return self._fetch_payloads("SELECT payload FROM indexing_jobs ORDER BY created_at DESC")

    def delete_job(self, job_id: str) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM indexing_jobs WHERE job_id = %s", (job_id,))
            conn.commit()

    def save_chat_session(self, session: dict) -> None:
        payload = dict(session)
        payload["owner_id"] = _owner_id(payload)
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO chat_sessions (session_id, owner_id, title, payload, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (session_id) DO UPDATE SET
                      title = EXCLUDED.title,
                      payload = EXCLUDED.payload,
                      updated_at = now()
                    WHERE chat_sessions.owner_id = EXCLUDED.owner_id
                    """,
                    (
                        payload["id"],
                        payload["owner_id"],
                        payload.get("title", ""),
                        self._jsonb(payload),
                    ),
                )
            conn.commit()

    def get_chat_session(self, session_id: str, owner_id: str = LEGACY_OWNER_ID) -> dict | None:
        return self._fetch_payload(
            "SELECT payload FROM chat_sessions WHERE session_id = %s AND owner_id = %s",
            (session_id, owner_id),
        )

    def list_chat_sessions(self, owner_id: str = LEGACY_OWNER_ID) -> list[dict]:
        return self._fetch_payloads(
            "SELECT payload FROM chat_sessions WHERE owner_id = %s ORDER BY updated_at DESC",
            (owner_id,),
        )

    def append_query_history(self, record: dict) -> None:
        payload = dict(record)
        payload["owner_id"] = _owner_id(payload)
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO query_history (query_id, owner_id, session_id, payload, created_at)
                    VALUES (%s, %s, %s, %s, NULLIF(%s, '')::timestamptz)
                    ON CONFLICT (query_id) DO UPDATE SET payload = EXCLUDED.payload
                    WHERE query_history.owner_id = EXCLUDED.owner_id
                    """,
                    (
                        payload["id"],
                        payload["owner_id"],
                        payload.get("session_id"),
                        self._jsonb(payload),
                        payload.get("timestamp", ""),
                    ),
                )
            conn.commit()

    def load_query_history(self, owner_id: str = LEGACY_OWNER_ID) -> list[dict]:
        return self._fetch_payloads(
            "SELECT payload FROM query_history WHERE owner_id = %s ORDER BY created_at DESC",
            (owner_id,),
        )

    def save_batch_meta(self, batch_id: str, meta: dict) -> None:
        payload = dict(meta)
        payload["owner_id"] = _owner_id(payload)
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO batch_qa_jobs (batch_id, owner_id, status, payload, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (batch_id) DO UPDATE SET
                      status = EXCLUDED.status,
                      payload = EXCLUDED.payload,
                      updated_at = now()
                    WHERE batch_qa_jobs.owner_id = EXCLUDED.owner_id
                    """,
                    (
                        batch_id,
                        payload["owner_id"],
                        payload.get("status", "submitted"),
                        self._jsonb(payload),
                    ),
                )
            conn.commit()

    def load_batch_meta(self, batch_id: str, owner_id: str = LEGACY_OWNER_ID) -> dict | None:
        return self._fetch_payload(
            "SELECT payload FROM batch_qa_jobs WHERE batch_id = %s AND owner_id = %s",
            (batch_id, owner_id),
        )

    def list_batch_metas(self, owner_id: str = LEGACY_OWNER_ID) -> list[dict]:
        return self._fetch_payloads(
            "SELECT payload FROM batch_qa_jobs WHERE owner_id = %s ORDER BY updated_at DESC",
            (owner_id,),
        )


_CACHE_KEY: tuple[str, str] | None = None
_CACHE_REPO: FileAppRepository | PostgresAppRepository | None = None


def get_app_repository() -> FileAppRepository | PostgresAppRepository:
    global _CACHE_KEY, _CACHE_REPO
    backend = os.getenv("GRAPHRAG_APP_BACKEND", os.getenv("GRAPHRAG_STORAGE_BACKEND", "filesystem")).strip().lower()
    if backend in {"json", "file", "local"}:
        backend = "filesystem"
    key = (backend, os.getenv("DATABASE_URL", ""))
    if _CACHE_REPO is not None and _CACHE_KEY == key:
        return _CACHE_REPO
    _CACHE_KEY = key
    _CACHE_REPO = PostgresAppRepository() if backend in {"postgres", "postgresql", "neon"} else FileAppRepository()
    return _CACHE_REPO


def reset_app_repository_cache() -> None:
    global _CACHE_KEY, _CACHE_REPO
    _CACHE_KEY = None
    _CACHE_REPO = None
