from __future__ import annotations

import importlib
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class SystemHealthTests(unittest.TestCase):
    def test_backend_python_candidates_cover_unix_windows_and_current_runtime(self):
        system = importlib.import_module("routers.system")
        backend_dir = Path("/demo/GraphRAGAgent/backend")

        candidates = [str(path) for path in system._backend_python_candidates(backend_dir)]

        self.assertIn("/demo/GraphRAGAgent/backend/.venv/bin/python", candidates)
        self.assertIn("/demo/GraphRAGAgent/backend/.venv/Scripts/python.exe", candidates)
        self.assertIn(sys.executable, candidates)

    def test_health_reports_parser_status_without_internal_configuration(self):
        system = importlib.import_module("routers.system")

        with patch.dict("os.environ", {"PARSER_MODE": "local", "MINERU_API_TOKEN": ""}, clear=False):
            response = asyncio.run(system.health_check())

        components = response.data["components"]
        self.assertIn("document_parser", components)
        parser = components["document_parser"]
        self.assertEqual(parser["mode"], "local")
        self.assertEqual(parser["active_parser"], "local")
        self.assertNotIn("mineru_configured", parser)
        self.assertNotIn("local_supported_formats", parser)

    def test_health_reports_ephemeral_storage_profile_for_tmp_data_dir(self):
        system = importlib.import_module("routers.system")

        with patch.object(system.fs, "_BASE", Path("/tmp/graphrag-data")):
            response = asyncio.run(system.health_check())

        storage = response.data["components"]["storage"]
        self.assertEqual(storage["mode"], "filesystem")
        self.assertEqual(storage["persistence"], "ephemeral")
        self.assertFalse(storage["persistent"])
        self.assertNotIn("data_dir", storage)
        self.assertNotIn("warning", storage)

    def test_health_component_sanitizer_removes_sensitive_details(self):
        system = importlib.import_module("routers.system")

        sanitized = system._sanitize_component({
            "status": "error",
            "backend": "postgres",
            "durable": True,
            "persistent": True,
            "base_url": "https://private.example",
            "path": "/private/path",
            "key_configured": True,
            "provider": "secret-provider",
            "model": "secret-model",
            "error": "raw connection error",
            "warning": "internal warning",
        })

        self.assertEqual(sanitized, {
            "status": "error",
            "backend": "postgres",
            "durable": True,
            "persistent": True,
        })

    def test_live_and_ready_health_endpoints(self):
        system = importlib.import_module("routers.system")

        class StubRepo:
            def health(self):
                return {"status": "ok", "backend": "neo4j"}

        with (
            patch.dict("os.environ", {
                "MINERU_API_TOKEN": "test-token",
                "PARSER_MODE": "auto",
            }, clear=False),
            patch.object(system, "LLM_API_KEY", "test-key"),
            patch.object(system, "_check_python_import", return_value={"status": "ok", "exists": True}),
            patch.object(system.graph_store, "get_graph_repository", return_value=StubRepo()),
        ):
            live = asyncio.run(system.live_check())
            ready = asyncio.run(system.ready_check())

        self.assertEqual(live.data["status"], "live")
        self.assertEqual(ready.data["status"], "ready")
        self.assertEqual(ready.data["components"]["graph_database"]["backend"], "neo4j")

    def test_lightrag_worker_health_requires_a_fresh_durable_heartbeat(self):
        system = importlib.import_module("routers.system")

        class QueueRepo:
            def profile(self):
                return {"backend": "upstash", "durable": True}

            def get_worker_heartbeat(self):
                return {
                    "worker_id": "railway-replica-1",
                    "version": "test-release",
                    "last_seen": 100,
                    "age_seconds": 5,
                    "ttl_seconds": 120,
                    "fresh": True,
                }

        with patch.object(system, "APP_VERSION", "test-release"):
            component = system._lightrag_worker_component(
                QueueRepo(),
                {"configured": True, "mode": "local", "components": {}},
            )

        self.assertEqual(component["status"], "ok")
        self.assertEqual(component["mode"], "active")
        self.assertTrue(component["durable"])
        self.assertEqual(component["heartbeat_age_seconds"], 5)
        self.assertNotEqual(component["worker_id"], "railway-replica-1")

    def test_lightrag_worker_health_fails_closed_for_missing_or_stale_heartbeat(self):
        system = importlib.import_module("routers.system")

        class QueueRepo:
            def __init__(self, heartbeat):
                self.heartbeat = heartbeat

            def profile(self):
                return {"backend": "upstash", "durable": True}

            def get_worker_heartbeat(self):
                return self.heartbeat

        enabled = {"configured": True, "mode": "local", "components": {}}
        missing = system._lightrag_worker_component(QueueRepo(None), enabled)
        stale = system._lightrag_worker_component(QueueRepo({
            "worker_id": "replica",
            "version": system.APP_VERSION,
            "last_seen": 100,
            "age_seconds": 121,
            "ttl_seconds": 120,
            "fresh": False,
        }), enabled)

        self.assertEqual((missing["status"], missing["mode"]), ("error", "missing"))
        self.assertEqual((stale["status"], stale["mode"]), ("error", "stale"))

    def test_backfill_health_reads_switch_maintenance_state_and_failures(self):
        system = importlib.import_module("routers.system")

        class AppRepo:
            def load_job_meta(self, _job_id):
                return {
                    "status": "failed",
                    "progress": {"failed": 1},
                    "updated_at": "2026-07-20T00:00:00+00:00",
                }

        class QueueRepo:
            def is_durable(self):
                return True

        documents = [
            {"indexes": {"lightrag": {"status": "done"}}},
            {"indexes": {"lightrag": {"status": "failed"}}},
        ]
        with patch.dict(os.environ, {
            "LIGHTRAG_BACKFILL_ON_START": "true",
            "LIGHTRAG_BACKFILL_ALL_TENANTS_ACK": "YES",
            "LIGHTRAG_BACKFILL_RELEASE_ID": "health-test",
        }, clear=False):
            component = system._lightrag_backfill_component(
                AppRepo(),
                QueueRepo(),
                documents,
                {"configured": True, "mode": "local"},
            )

        self.assertEqual(component["status"], "error")
        self.assertEqual(component["mode"], "failed")
        self.assertEqual(component["maintenance_status"], "failed")
        self.assertEqual(component["failed"], 1)
        self.assertEqual(component["done"], 1)

    def test_backfill_health_is_disabled_when_startup_switch_is_off(self):
        system = importlib.import_module("routers.system")

        class AppRepo:
            def load_job_meta(self, _job_id):
                return None

        class QueueRepo:
            def is_durable(self):
                return True

        with patch.dict(os.environ, {
            "LIGHTRAG_BACKFILL_ON_START": "false",
            "LIGHTRAG_BACKFILL_ALL_TENANTS_ACK": "",
        }, clear=False):
            component = system._lightrag_backfill_component(
                AppRepo(),
                QueueRepo(),
                [],
                {"configured": True, "mode": "local"},
            )

        self.assertEqual(component["status"], "ok")
        self.assertEqual(component["mode"], "disabled")
        self.assertFalse(component["enabled"])
        self.assertFalse(component["configured"])


if __name__ == "__main__":
    unittest.main()
