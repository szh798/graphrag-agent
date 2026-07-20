from __future__ import annotations

import unittest
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "lightrag"


class LightRAGDeploymentAssetTests(unittest.TestCase):
    def test_shared_image_is_pinned_and_runs_internal_runtime(self):
        dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
        lock = (DEPLOY / "requirements.lock").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        api_config = (DEPLOY / "railway-api.toml").read_text(encoding="utf-8")
        worker_config = (DEPLOY / "railway-worker.toml").read_text(encoding="utf-8")
        api_toml = tomllib.loads(api_config)
        worker_toml = tomllib.loads(worker_config)

        self.assertIn("lightrag-hku==1.5.4", lock)
        self.assertIn("COPY backend/lightrag_integration", dockerfile)
        self.assertIn("lightrag_integration.runtime:app", dockerfile)
        self.assertIn('healthcheckPath = "/live"', api_config)
        self.assertIn('startCommand = "python -m scripts.worker"', worker_config)
        self.assertIn('dockerfilePath = "deploy/lightrag/Dockerfile"', api_config)
        self.assertIn('dockerfilePath = "deploy/lightrag/Dockerfile"', worker_config)
        self.assertEqual(api_toml["deploy"]["healthcheckPath"], "/live")
        self.assertEqual(worker_toml["deploy"]["restartPolicyType"], "ON_FAILURE")
        self.assertIn("**/.env", dockerignore)
        self.assertIn("backend/data", dockerignore)

    def test_environment_inventory_keeps_database_credentials_off_vercel(self):
        inventory = (DEPLOY / "env.example").read_text(encoding="utf-8")
        required = {
            "LIGHTRAG_BASE_URL",
            "LIGHTRAG_HMAC_SECRET",
            "LIGHTRAG_WORKSPACE_SECRET",
            "LIGHTRAG_HMAC_MAX_AGE_SECONDS",
            "LIGHTRAG_MAX_GRAPH_NODES",
            "LIGHTRAG_MAX_GRAPH_EDGES",
            "LIGHTRAG_WORKSPACE_CACHE_MAX",
            "LIGHTRAG_HEALTH_PROBE_TIMEOUT_SECONDS",
            "LIGHTRAG_REQUIRE_DURABLE_QUEUE",
            "GRAPHRAG_GRAPH_BACKEND",
            "POSTGRES_SSL_MODE",
            "NEO4J_URI",
            "RERANK_MODEL",
        }
        for name in required:
            self.assertIn(f"{name}=", inventory)
        self.assertIn("Vercel public gateway only", inventory)
        self.assertIn("Dedicated Neon retrieval database (Railway only)", inventory)
        self.assertNotIn("https://open.feishu.cn/open-apis/bot/v2/hook/", inventory)


if __name__ == "__main__":
    unittest.main()
