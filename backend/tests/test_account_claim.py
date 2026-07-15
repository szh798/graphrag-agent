from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from identity import RequestIdentity, require_authenticated_identity
from routers import account as account_router
from storage.account_repository import PostgresAccountRepository


VISITOR_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def account_identity() -> RequestIdentity:
    return RequestIdentity(
        authenticated=True,
        actor_id="user_123",
        tenant_id="user:user_123",
        role="owner",
        visitor_id=VISITOR_ID,
    )


class FakeCursor:
    def __init__(self) -> None:
        self.rowcount = 0
        self._rows: list[dict] = []
        self.executions: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query: str, params: tuple = ()) -> None:
        normalized = " ".join(query.split())
        self.executions.append((normalized, params))
        self._rows = []
        if normalized.startswith("UPDATE app_documents"):
            self._rows = [{"doc_id": "doc_1"}]
            self.rowcount = 1
        elif normalized.startswith("UPDATE indexing_jobs"):
            self.rowcount = 1
        elif "UPDATE chat_sessions" in normalized:
            self.rowcount = 2
        elif "UPDATE query_history" in normalized:
            self.rowcount = 3
        elif "UPDATE batch_qa_jobs" in normalized:
            self.rowcount = 4
        else:
            self.rowcount = 0

    def fetchall(self) -> list[dict]:
        return list(self._rows)


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.committed = True


class AccountClaimRepositoryTests(unittest.TestCase):
    def test_claim_moves_anonymous_records_to_authenticated_tenant(self):
        repo = PostgresAccountRepository()
        repo._schema_ready = True
        connection = FakeConnection()

        with patch.object(repo, "_connect", return_value=connection):
            result = repo.claim_visitor_data(account_identity())

        self.assertTrue(connection.committed)
        self.assertEqual(result["tenant_id"], "user:user_123")
        self.assertEqual(result["claimed"], {
            "documents": 1,
            "indexing_jobs": 1,
            "sessions": 2,
            "queries": 3,
            "batches": 4,
        })
        for query, params in connection.cursor_instance.executions:
            if query.startswith("UPDATE") and "indexing_jobs" not in query:
                self.assertEqual(params[-1], VISITOR_ID)
        query_history_update = next(
            query for query, _params in connection.cursor_instance.executions
            if query.startswith("UPDATE query_history")
        )
        self.assertNotIn("updated_at", query_history_update)

    def test_claim_without_visitor_is_an_idempotent_noop(self):
        identity = RequestIdentity(
            authenticated=True,
            actor_id="user_123",
            tenant_id="user:user_123",
            role="owner",
        )
        repo = PostgresAccountRepository()

        result = repo.claim_visitor_data(identity)

        self.assertEqual(sum(result["claimed"].values()), 0)


class AccountClaimRouteTests(unittest.TestCase):
    def test_authenticated_claim_syncs_identity_and_records_audit(self):
        identity = account_identity()

        class FakeRepository:
            def __init__(self) -> None:
                self.synced = False
                self.audit: tuple | None = None

            def sync_identity(self, received: RequestIdentity) -> None:
                self.synced = received == identity

            def claim_visitor_data(self, _received: RequestIdentity) -> dict:
                return {
                    "tenant_id": identity.tenant_id,
                    "claimed": {"documents": 1, "indexing_jobs": 0, "sessions": 0, "queries": 0, "batches": 0},
                }

            def record_audit(self, *args) -> None:
                self.audit = args

        repo = FakeRepository()
        app = FastAPI()
        app.include_router(account_router.router)
        app.dependency_overrides[require_authenticated_identity] = lambda: identity

        with patch.object(account_router, "get_account_repository", return_value=repo):
            response = TestClient(app).post("/account/claim-visitor-data")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["claimed"]["documents"], 1)
        self.assertTrue(repo.synced)
        self.assertIsNotNone(repo.audit)


if __name__ == "__main__":
    unittest.main()
