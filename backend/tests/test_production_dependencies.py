from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class ProductionDependencyTests(unittest.TestCase):
    def test_default_dependency_repositories_keep_local_demo_ready(self):
        app_repository = importlib.import_module("storage.app_repository")
        blob_repository = importlib.import_module("storage.blob_repository")
        queue_repository = importlib.import_module("storage.queue_repository")

        with patch.dict("os.environ", {
            "GRAPHRAG_APP_BACKEND": "filesystem",
            "GRAPHRAG_BLOB_BACKEND": "filesystem",
            "GRAPHRAG_QUEUE_BACKEND": "local_thread",
        }, clear=False):
            app_repository.reset_app_repository_cache()
            blob_repository.reset_blob_repository_cache()
            queue_repository.reset_queue_repository_cache()

            self.assertEqual(app_repository.get_app_repository().health()["status"], "ok")
            self.assertEqual(blob_repository.get_blob_repository().health()["status"], "ok")
            self.assertEqual(queue_repository.get_queue_repository().health()["status"], "ok")

    def test_production_backends_report_missing_configuration(self):
        app_repository = importlib.import_module("storage.app_repository")
        blob_repository = importlib.import_module("storage.blob_repository")
        queue_repository = importlib.import_module("storage.queue_repository")

        with patch.dict("os.environ", {
            "GRAPHRAG_APP_BACKEND": "postgres",
            "GRAPHRAG_BLOB_BACKEND": "vercel_blob",
            "GRAPHRAG_QUEUE_BACKEND": "upstash",
            "DATABASE_URL": "",
            "BLOB_READ_WRITE_TOKEN": "",
            "UPSTASH_REDIS_REST_URL": "",
            "UPSTASH_REDIS_REST_TOKEN": "",
        }, clear=False):
            app_repository.reset_app_repository_cache()
            blob_repository.reset_blob_repository_cache()
            queue_repository.reset_queue_repository_cache()

            self.assertEqual(app_repository.get_app_repository().health()["status"], "error")
            self.assertEqual(blob_repository.get_blob_repository().health()["status"], "error")
            self.assertEqual(queue_repository.get_queue_repository().health()["status"], "error")

    def test_system_ready_includes_industrial_dependency_components(self):
        system = importlib.import_module("routers.system")

        with (
            patch.object(system.graph_store.get_graph_repository(), "health", return_value={"status": "ok", "backend": "neo4j"}),
            patch.object(system.app_store.get_app_repository(), "health", return_value={"status": "ok", "backend": "postgres"}),
            patch.object(system.blob_store.get_blob_repository(), "health", return_value={"status": "ok", "backend": "vercel_blob"}),
            patch.object(system.queue_store.get_queue_repository(), "health", return_value={"status": "ok", "backend": "upstash"}),
        ):
            response = asyncio.run(system.ready_check())

        components = response.data["components"]
        self.assertEqual(response.data["status"], "ready")
        self.assertEqual(components["graph_database"]["backend"], "neo4j")
        self.assertEqual(components["app_database"]["backend"], "postgres")
        self.assertEqual(components["blob_storage"]["backend"], "vercel_blob")
        self.assertEqual(components["task_queue"]["backend"], "upstash")


if __name__ == "__main__":
    unittest.main()
