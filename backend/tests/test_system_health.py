from __future__ import annotations

import importlib
import asyncio
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


if __name__ == "__main__":
    unittest.main()
