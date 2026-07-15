from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class BootstrapProductionTests(unittest.TestCase):
    def test_bootstrap_applies_available_schemas_and_reports_health(self):
        from scripts import bootstrap_production as bootstrap

        class SchemaRepo:
            def __init__(self, backend: str):
                self.backend = backend
                self.schema_applied = False

            def ensure_schema(self):
                self.schema_applied = True

            def health(self):
                return {"status": "ok", "backend": self.backend}

        graph_repo = SchemaRepo("neo4j")
        app_repo = SchemaRepo("postgres")
        blob_repo = SchemaRepo("vercel_blob")
        queue_repo = SchemaRepo("upstash")
        account_repo = SchemaRepo("postgres_accounts")

        with (
            patch.object(bootstrap.graph_store, "get_graph_repository", return_value=graph_repo),
            patch.object(bootstrap.app_store, "get_app_repository", return_value=app_repo),
            patch.object(bootstrap.blob_store, "get_blob_repository", return_value=blob_repo),
            patch.object(bootstrap.queue_store, "get_queue_repository", return_value=queue_repo),
            patch.object(bootstrap.account_store, "get_account_repository", return_value=account_repo),
        ):
            result = bootstrap.bootstrap_production(apply_schema=True)

        self.assertTrue(graph_repo.schema_applied)
        self.assertTrue(app_repo.schema_applied)
        self.assertTrue(account_repo.schema_applied)
        self.assertTrue(result["ready"])
        self.assertEqual(result["schema"]["graph_database"], "applied")
        self.assertEqual(result["schema"]["app_database"], "applied")
        self.assertEqual(result["schema"]["account_database"], "applied")
        self.assertEqual(result["components"]["task_queue"]["backend"], "upstash")


if __name__ == "__main__":
    unittest.main()
