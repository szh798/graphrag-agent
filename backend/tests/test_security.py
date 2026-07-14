from __future__ import annotations

import asyncio
import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.responses import JSONResponse


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


async def _call_middleware(middleware, path: str, headers: dict[str, str] | None = None):
    messages: list[dict] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (name.lower().encode(), value.encode())
            for name, value in (headers or {}).items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await middleware(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, json.loads(body)


async def _ok_app(scope, receive, send):
    await JSONResponse({"ok": True})(scope, receive, send)


class ProxyAuthMiddlewareTests(unittest.TestCase):
    def test_protected_api_requires_matching_secret(self):
        security = importlib.import_module("security")
        middleware = security.ProxyAuthMiddleware(_ok_app, secret="correct-secret", require_auth=False)

        missing_status, missing_body = asyncio.run(_call_middleware(middleware, "/api/v1/documents"))
        wrong_status, _ = asyncio.run(_call_middleware(
            middleware,
            "/api/v1/documents",
            {security.PROXY_SECRET_HEADER: "wrong-secret"},
        ))
        good_status, good_body = asyncio.run(_call_middleware(
            middleware,
            "/api/v1/documents",
            {security.PROXY_SECRET_HEADER: "correct-secret"},
        ))

        self.assertEqual(missing_status, 401)
        self.assertEqual(missing_body["msg"], "Unauthorized")
        self.assertEqual(wrong_status, 401)
        self.assertEqual(good_status, 200)
        self.assertTrue(good_body["ok"])

    def test_live_health_is_public_even_when_auth_is_required(self):
        security = importlib.import_module("security")
        middleware = security.ProxyAuthMiddleware(_ok_app, secret="correct-secret", require_auth=True)

        status, body = asyncio.run(_call_middleware(middleware, "/api/v1/health/live"))

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_required_auth_without_secret_fails_closed(self):
        security = importlib.import_module("security")
        middleware = security.ProxyAuthMiddleware(_ok_app, secret="", require_auth=True)

        status, body = asyncio.run(_call_middleware(middleware, "/api/v1/health/ready"))

        self.assertEqual(status, 503)
        self.assertEqual(body["msg"], "Service unavailable")

    def test_auth_is_opt_in_for_local_development(self):
        security = importlib.import_module("security")
        middleware = security.ProxyAuthMiddleware(_ok_app, secret="", require_auth=False)

        status, body = asyncio.run(_call_middleware(middleware, "/api/v1/documents"))

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


class MainSecurityConfigurationTests(unittest.TestCase):
    def test_cors_defaults_to_local_origins(self):
        main = importlib.import_module("main")
        with patch.dict("os.environ", {"ALLOWED_ORIGINS": ""}, clear=False):
            origins, credentials = main._cors_settings()

        self.assertIn("http://localhost:5173", origins)
        self.assertNotIn("*", origins)
        self.assertTrue(credentials)

    def test_cors_wildcard_is_never_credentialed(self):
        main = importlib.import_module("main")
        with patch.dict("os.environ", {"ALLOWED_ORIGINS": "*"}, clear=False):
            origins, credentials = main._cors_settings()

        self.assertEqual(origins, ["*"])
        self.assertFalse(credentials)

    def test_production_disables_all_api_documentation_routes(self):
        main = importlib.import_module("main")
        try:
            with patch.dict("os.environ", {"VERCEL_ENV": "production", "ENVIRONMENT": "development"}, clear=False):
                production_main = importlib.reload(main)
                self.assertTrue(production_main._is_production())
                self.assertIsNone(production_main.app.docs_url)
                self.assertIsNone(production_main.app.redoc_url)
                self.assertIsNone(production_main.app.openapi_url)
        finally:
            importlib.reload(main)


if __name__ == "__main__":
    unittest.main()
