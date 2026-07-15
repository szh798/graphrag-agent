"""Production account, tenant, usage, audit, and operations repository."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from identity import RequestIdentity


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresAccountRepository:
    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL", os.getenv("POSTGRES_URL", "")).strip()
        self._schema_ready = False

    def _connect(self):
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required for account features")
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.database_url, connect_timeout=5, row_factory=dict_row)

    @staticmethod
    def _jsonb(value: dict | list):
        from psycopg.types.json import Jsonb

        return Jsonb(value)

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        statements = [
            """
            CREATE TABLE IF NOT EXISTS account_profiles (
              user_id TEXT PRIMARY KEY,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tenant_profiles (
              tenant_id TEXT PRIMARY KEY,
              organization_id TEXT,
              slug TEXT,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tenant_memberships (
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              role TEXT NOT NULL,
              permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              PRIMARY KEY (tenant_id, user_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS tenant_memberships_user_idx ON tenant_memberships(user_id)",
            """
            CREATE TABLE IF NOT EXISTS usage_events (
              event_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              operation TEXT NOT NULL,
              provider TEXT NOT NULL DEFAULT '',
              model TEXT NOT NULL DEFAULT '',
              input_tokens BIGINT NOT NULL DEFAULT 0,
              output_tokens BIGINT NOT NULL DEFAULT 0,
              cost_microcny BIGINT NOT NULL DEFAULT 0,
              request_id TEXT,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cost_microcny BIGINT NOT NULL DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS usage_events_tenant_created_idx ON usage_events(tenant_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS usage_events_user_created_idx ON usage_events(user_id, created_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS ops_events (
              event_id TEXT PRIMARY KEY,
              severity TEXT NOT NULL,
              source TEXT NOT NULL,
              event_type TEXT NOT NULL,
              request_id TEXT,
              tenant_id TEXT,
              actor_id TEXT,
              fingerprint TEXT NOT NULL,
              message TEXT NOT NULL,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS ops_events_created_idx ON ops_events(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ops_events_fingerprint_created_idx ON ops_events(fingerprint, created_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
              audit_id BIGSERIAL PRIMARY KEY,
              owner_id TEXT NOT NULL DEFAULT 'default',
              event_type TEXT NOT NULL,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_id TEXT",
            "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS tenant_id TEXT",
            "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS role TEXT",
            "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS request_id TEXT",
            "CREATE INDEX IF NOT EXISTS audit_logs_tenant_created_idx ON audit_logs(tenant_id, created_at DESC)",
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
            conn.commit()
        self._schema_ready = True

    def sync_identity(self, identity: RequestIdentity) -> None:
        if not identity.authenticated:
            return
        self.ensure_schema()
        account_payload = {
            "user_id": identity.actor_id,
            "last_seen_at": _now(),
        }
        tenant_payload = {
            "tenant_id": identity.tenant_id,
            "organization_id": identity.organization_id,
            "organization_slug": identity.organization_slug,
            "last_seen_at": _now(),
        }
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO account_profiles(user_id, payload, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT(user_id) DO UPDATE SET payload = account_profiles.payload || EXCLUDED.payload, updated_at = now()
                    """,
                    (identity.actor_id, self._jsonb(account_payload)),
                )
                cur.execute(
                    """
                    INSERT INTO tenant_profiles(tenant_id, organization_id, slug, payload, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT(tenant_id) DO UPDATE SET
                      organization_id = EXCLUDED.organization_id,
                      slug = EXCLUDED.slug,
                      payload = tenant_profiles.payload || EXCLUDED.payload,
                      updated_at = now()
                    """,
                    (identity.tenant_id, identity.organization_id, identity.organization_slug, self._jsonb(tenant_payload)),
                )
                cur.execute(
                    """
                    INSERT INTO tenant_memberships(tenant_id, user_id, role, permissions, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT(tenant_id, user_id) DO UPDATE SET
                      role = EXCLUDED.role,
                      permissions = EXCLUDED.permissions,
                      updated_at = now()
                    """,
                    (identity.tenant_id, identity.actor_id, identity.role, self._jsonb(list(identity.permissions))),
                )
            conn.commit()

    def claim_visitor_data(self, identity: RequestIdentity) -> dict:
        """Move this browser's anonymous records into the authenticated tenant."""
        claimed = {
            "documents": 0,
            "indexing_jobs": 0,
            "sessions": 0,
            "queries": 0,
            "batches": 0,
        }
        visitor_id = identity.visitor_id
        if not identity.authenticated or not visitor_id or visitor_id == identity.tenant_id:
            return {"tenant_id": identity.tenant_id, "claimed": claimed}

        self.ensure_schema()
        ownership = self._jsonb({
            "owner_id": identity.tenant_id,
            "actor_id": identity.actor_id,
        })
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app_documents
                    SET owner_id = %s, payload = payload || %s, updated_at = now()
                    WHERE owner_id = %s
                    RETURNING doc_id
                    """,
                    (identity.tenant_id, ownership, visitor_id),
                )
                document_ids = [str(row["doc_id"]) for row in cur.fetchall()]
                claimed["documents"] = len(document_ids)

                if document_ids:
                    cur.execute(
                        """
                        UPDATE indexing_jobs
                        SET payload = payload || %s, updated_at = now()
                        WHERE doc_id = ANY(%s)
                        """,
                        (ownership, document_ids),
                    )
                    claimed["indexing_jobs"] = cur.rowcount

                for table, key in (
                    ("chat_sessions", "sessions"),
                    ("query_history", "queries"),
                    ("batch_qa_jobs", "batches"),
                ):
                    cur.execute(
                        f"""
                        UPDATE {table}
                        SET owner_id = %s, payload = payload || %s, updated_at = now()
                        WHERE owner_id = %s
                        """,
                        (identity.tenant_id, ownership, visitor_id),
                    )
                    claimed[key] = cur.rowcount
            conn.commit()
        return {"tenant_id": identity.tenant_id, "claimed": claimed}

    def record_audit(self, identity: RequestIdentity, event_type: str, request_id: str, payload: dict | None = None) -> None:
        if not identity.authenticated:
            return
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_logs(owner_id, actor_id, tenant_id, role, request_id, event_type, payload)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        identity.tenant_id,
                        identity.actor_id,
                        identity.tenant_id,
                        identity.role,
                        request_id,
                        event_type,
                        self._jsonb(payload or {}),
                    ),
                )
            conn.commit()

    def record_usage(
        self,
        *,
        tenant_id: str,
        user_id: str,
        operation: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_microcny: int,
        request_id: str,
        payload: dict | None = None,
    ) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO usage_events(
                      event_id, tenant_id, user_id, operation, provider, model,
                      input_tokens, output_tokens, cost_microcny, request_id, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        f"use_{uuid.uuid4().hex}", tenant_id, user_id, operation, provider, model,
                        max(0, int(input_tokens)), max(0, int(output_tokens)), max(0, int(cost_microcny)),
                        request_id, self._jsonb(payload or {}),
                    ),
                )
            conn.commit()

    def usage_summary(self, tenant_id: str, user_id: str | None = None, days: int = 30) -> dict:
        self.ensure_schema()
        days = min(max(int(days), 1), 366)
        where = "tenant_id = %s AND created_at >= now() - (%s * interval '1 day')"
        params: list[Any] = [tenant_id, days]
        if user_id:
            where += " AND user_id = %s"
            params.append(user_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                      count(*) AS events,
                      COALESCE(sum(input_tokens), 0) AS input_tokens,
                      COALESCE(sum(output_tokens), 0) AS output_tokens,
                      COALESCE(sum(cost_microcny), 0) AS cost_microcny
                    FROM usage_events WHERE {where}
                    """,
                    tuple(params),
                )
                total = dict(cur.fetchone())
                cur.execute(
                    f"""
                    SELECT operation, provider, model, count(*) AS events,
                      sum(input_tokens) AS input_tokens,
                      sum(output_tokens) AS output_tokens,
                      sum(cost_microcny) AS cost_microcny
                    FROM usage_events WHERE {where}
                    GROUP BY operation, provider, model
                    ORDER BY cost_microcny DESC, events DESC
                    """,
                    tuple(params),
                )
                breakdown = [dict(row) for row in cur.fetchall()]
        total["cost_cny"] = round(int(total.pop("cost_microcny")) / 1_000_000, 6)
        for row in breakdown:
            row["cost_cny"] = round(int(row.pop("cost_microcny") or 0) / 1_000_000, 6)
        input_price = float(os.getenv("LLM_INPUT_CNY_PER_1M_TOKENS", "0") or 0)
        output_price = float(os.getenv("LLM_OUTPUT_CNY_PER_1M_TOKENS", "0") or 0)
        return {
            "days": days,
            "pricing_configured": input_price > 0 or output_price > 0,
            "currency": "CNY",
            "total": total,
            "breakdown": breakdown,
        }

    def _payloads(self, cur, table: str, id_column: str, tenant_id: str) -> list[dict]:
        cur.execute(f"SELECT {id_column}, payload FROM {table} WHERE owner_id = %s ORDER BY 1", (tenant_id,))
        return [dict(row["payload"]) for row in cur.fetchall() if row.get("payload")]

    def export_tenant(self, identity: RequestIdentity) -> dict:
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                documents = self._payloads(cur, "app_documents", "doc_id", identity.tenant_id)
                sessions = self._payloads(cur, "chat_sessions", "session_id", identity.tenant_id)
                batches = self._payloads(cur, "batch_qa_jobs", "batch_id", identity.tenant_id)
                queries = self._payloads(cur, "query_history", "query_id", identity.tenant_id)
                cur.execute(
                    """
                    SELECT operation, provider, model, input_tokens, output_tokens,
                           cost_microcny, request_id, payload, created_at
                    FROM usage_events WHERE tenant_id = %s ORDER BY created_at
                    """,
                    (identity.tenant_id,),
                )
                usage = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT actor_id, role, request_id, event_type, payload, created_at
                    FROM audit_logs WHERE tenant_id = %s ORDER BY created_at
                    """,
                    (identity.tenant_id,),
                )
                audit = [dict(row) for row in cur.fetchall()]
        for collection in (usage, audit):
            for row in collection:
                if row.get("created_at"):
                    row["created_at"] = row["created_at"].isoformat()
        return {
            "exported_at": _now(),
            "identity": identity.public_dict(),
            "documents": documents,
            "sessions": sessions,
            "queries": queries,
            "batches": batches,
            "usage": usage,
            "audit": audit,
        }

    def export_user(self, identity: RequestIdentity) -> dict:
        """Export one user's account and authored records inside a shared tenant."""
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM account_profiles WHERE user_id = %s", (identity.actor_id,))
                account_row = cur.fetchone()
                account = dict(account_row["payload"]) if account_row and account_row.get("payload") else {}
                collections: dict[str, list[dict]] = {}
                for table, name in (
                    ("chat_sessions", "sessions"),
                    ("query_history", "queries"),
                    ("batch_qa_jobs", "batches"),
                ):
                    cur.execute(
                        f"SELECT payload FROM {table} WHERE owner_id = %s AND payload->>'actor_id' = %s ORDER BY created_at",
                        (identity.tenant_id, identity.actor_id),
                    )
                    collections[name] = [dict(row["payload"]) for row in cur.fetchall() if row.get("payload")]
                cur.execute(
                    """
                    SELECT operation, provider, model, input_tokens, output_tokens,
                           cost_microcny, request_id, payload, created_at
                    FROM usage_events WHERE user_id = %s ORDER BY created_at
                    """,
                    (identity.actor_id,),
                )
                usage = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT role, request_id, event_type, payload, created_at
                    FROM audit_logs WHERE actor_id = %s ORDER BY created_at
                    """,
                    (identity.actor_id,),
                )
                audit = [dict(row) for row in cur.fetchall()]
        for collection in (usage, audit):
            for row in collection:
                if row.get("created_at"):
                    row["created_at"] = row["created_at"].isoformat()
        return {
            "exported_at": _now(),
            "identity": identity.public_dict(),
            "account": account,
            **collections,
            "usage": usage,
            "audit": audit,
        }

    def tenant_document_ids(self, tenant_id: str) -> list[str]:
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT doc_id FROM app_documents WHERE owner_id = %s", (tenant_id,))
                return [str(row["doc_id"]) for row in cur.fetchall()]

    def delete_personal_data(self, identity: RequestIdentity) -> dict:
        self.ensure_schema()
        deleted: dict[str, int] = {}
        with self._connect() as conn:
            with conn.cursor() as cur:
                for table, key in (
                    ("query_history", "queries"),
                    ("batch_qa_jobs", "batches"),
                    ("chat_sessions", "sessions"),
                ):
                    cur.execute(
                        f"DELETE FROM {table} WHERE owner_id = %s AND payload->>'actor_id' = %s",
                        (identity.tenant_id, identity.actor_id),
                    )
                    deleted[key] = cur.rowcount
                cur.execute("DELETE FROM usage_events WHERE user_id = %s", (identity.actor_id,))
                deleted["usage_events"] = cur.rowcount
                cur.execute("DELETE FROM audit_logs WHERE actor_id = %s", (identity.actor_id,))
                deleted["audit_events"] = cur.rowcount
                cur.execute("DELETE FROM tenant_memberships WHERE user_id = %s", (identity.actor_id,))
                deleted["memberships"] = cur.rowcount
                cur.execute("DELETE FROM account_profiles WHERE user_id = %s", (identity.actor_id,))
                deleted["account_profiles"] = cur.rowcount
            conn.commit()
        return {"scope": "personal", "deleted": deleted}

    def delete_tenant_data(self, tenant_id: str) -> dict:
        self.ensure_schema()
        deleted: dict[str, int] = {}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM indexing_jobs WHERE doc_id IN (SELECT doc_id FROM app_documents WHERE owner_id = %s)",
                    (tenant_id,),
                )
                deleted["indexing_jobs"] = cur.rowcount
                for table, column, key in (
                    ("query_history", "owner_id", "queries"),
                    ("batch_qa_jobs", "owner_id", "batches"),
                    ("chat_sessions", "owner_id", "sessions"),
                    ("app_documents", "owner_id", "documents"),
                    ("usage_events", "tenant_id", "usage_events"),
                    ("audit_logs", "tenant_id", "audit_events"),
                    ("tenant_memberships", "tenant_id", "memberships"),
                    ("tenant_profiles", "tenant_id", "tenant_profiles"),
                ):
                    cur.execute(f"DELETE FROM {table} WHERE {column} = %s", (tenant_id,))
                    deleted[key] = cur.rowcount
            conn.commit()
        return {"scope": "tenant", "deleted": deleted}

    def record_ops_event(
        self,
        *,
        severity: str,
        source: str,
        event_type: str,
        request_id: str,
        message: str,
        tenant_id: str | None = None,
        actor_id: str | None = None,
        payload: dict | None = None,
    ) -> str:
        self.ensure_schema()
        normalized_message = " ".join(str(message).split())[:500]
        fingerprint = hashlib.sha256(f"{source}|{event_type}|{normalized_message}".encode()).hexdigest()[:24]
        event_id = f"ops_{uuid.uuid4().hex}"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ops_events(
                      event_id, severity, source, event_type, request_id, tenant_id,
                      actor_id, fingerprint, message, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event_id, severity, source, event_type, request_id, tenant_id,
                        actor_id, fingerprint, normalized_message, self._jsonb(payload or {}),
                    ),
                )
            conn.commit()
        return event_id

    def ops_summary(self, hours: int = 24) -> dict:
        self.ensure_schema()
        hours = min(max(int(hours), 1), 24 * 30)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*) AS total,
                      count(*) FILTER (WHERE severity = 'error') AS errors,
                      count(*) FILTER (WHERE severity = 'warning') AS warnings,
                      count(DISTINCT fingerprint) AS unique_issues
                    FROM ops_events
                    WHERE created_at >= now() - (%s * interval '1 hour')
                    """,
                    (hours,),
                )
                total = dict(cur.fetchone())
                cur.execute(
                    """
                    SELECT fingerprint, source, event_type, severity, message,
                      count(*) AS occurrences, max(created_at) AS last_seen
                    FROM ops_events
                    WHERE created_at >= now() - (%s * interval '1 hour')
                    GROUP BY fingerprint, source, event_type, severity, message
                    ORDER BY occurrences DESC, last_seen DESC
                    LIMIT 50
                    """,
                    (hours,),
                )
                issues = [dict(row) for row in cur.fetchall()]
        for row in issues:
            if row.get("last_seen"):
                row["last_seen"] = row["last_seen"].isoformat()
        return {"hours": hours, "totals": total, "issues": issues}


_REPOSITORY: PostgresAccountRepository | None = None


def get_account_repository() -> PostgresAccountRepository:
    global _REPOSITORY
    database_url = os.getenv("DATABASE_URL", os.getenv("POSTGRES_URL", "")).strip()
    if _REPOSITORY is None or _REPOSITORY.database_url != database_url:
        _REPOSITORY = PostgresAccountRepository()
    return _REPOSITORY


def reset_account_repository_cache() -> None:
    global _REPOSITORY
    _REPOSITORY = None
