from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from lightrag_integration.errors import (  # noqa: E402
    LightRAGAuthenticationError,
    LightRAGConfigurationError,
)
from lightrag_integration.security import (  # noqa: E402
    NonceReplayCache,
    sign_request,
    verify_request,
    workspace_key,
)


class LightRAGSecurityTests(unittest.TestCase):
    def test_workspace_is_deterministic_opaque_and_secret_scoped(self):
        tenant = "org:customer-secret-name"
        one = workspace_key(tenant, secret="a" * 32)
        two = workspace_key(tenant, secret="a" * 32)
        other = workspace_key(tenant, secret="b" * 32)

        self.assertEqual(one, two)
        self.assertNotEqual(one, other)
        self.assertTrue(one.startswith("ws_"))
        self.assertNotIn("customer", one)
        self.assertEqual(len(one), 43)

    def test_workspace_fails_closed_without_a_strong_secret(self):
        with patch.dict(os.environ, {"LIGHTRAG_WORKSPACE_SECRET": "short"}, clear=False):
            with self.assertRaises(LightRAGConfigurationError):
                workspace_key("tenant")

    def test_request_signature_binds_method_path_body_and_rejects_replay(self):
        secret = "internal-signing-secret-" + "x" * 32
        body = b'{"workspace":"ws_demo"}'
        headers = sign_request(
            "POST",
            "/internal/v1/query",
            body,
            secret=secret,
            timestamp=1_000,
            nonce="unique-nonce-value-123",
        )
        replay_cache = NonceReplayCache()
        local_nonce_env = {
            "LIGHTRAG_REQUIRE_DISTRIBUTED_NONCE": "false",
            "LIGHTRAG_NONCE_REDIS_REST_URL": "",
            "LIGHTRAG_NONCE_REDIS_REST_TOKEN": "",
            "UPSTASH_REDIS_REST_URL": "",
            "UPSTASH_REDIS_REST_TOKEN": "",
        }
        with patch.dict(os.environ, local_nonce_env, clear=False):
            verify_request(
                "POST",
                "/internal/v1/query",
                body,
                headers,
                secret=secret,
                now=1_001,
                max_age_seconds=60,
                replay_cache=replay_cache,
            )

            with self.assertRaises(LightRAGAuthenticationError):
                verify_request(
                    "POST",
                    "/internal/v1/query",
                    body,
                    headers,
                    secret=secret,
                    now=1_002,
                    max_age_seconds=60,
                    replay_cache=replay_cache,
                )
            with self.assertRaises(LightRAGAuthenticationError):
                verify_request(
                    "POST",
                    "/internal/v1/query",
                    b"altered",
                    headers,
                    secret=secret,
                    now=1_002,
                    max_age_seconds=60,
                )

    def test_distributed_nonce_fence_hashes_nonce_and_rejects_cross_process_replay(self):
        from lightrag_integration import security

        calls: list[str] = []

        class Response:
            def __init__(self, result):
                self.result = result

            def raise_for_status(self):
                return None

            def json(self):
                return {"result": self.result}

        responses = iter((Response("OK"), Response(None)))

        def post(url, **_kwargs):
            calls.append(url)
            return next(responses)

        env = {
            "LIGHTRAG_REQUIRE_DISTRIBUTED_NONCE": "true",
            "LIGHTRAG_NONCE_REDIS_REST_URL": "https://redis.example",
            "LIGHTRAG_NONCE_REDIS_REST_TOKEN": "secret-token",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(security.requests, "post", side_effect=post):
            self.assertTrue(NonceReplayCache().consume("private-nonce-value", expires_at=160, now=100))
            self.assertFalse(NonceReplayCache().consume("private-nonce-value", expires_at=160, now=100))

        self.assertEqual(len(calls), 2)
        self.assertNotIn("private-nonce-value", calls[0])
        self.assertIn("NX", calls[0])

    def test_signature_rejects_wrong_method_path_and_stale_timestamp(self):
        secret = "internal-signing-secret-" + "s" * 32
        body = b'{"workspace":"ws_demo"}'
        headers = sign_request(
            "POST",
            "/internal/v1/query",
            body,
            secret=secret,
            timestamp=1_000,
            nonce="method-path-stale-nonce",
        )

        with self.assertRaises(LightRAGAuthenticationError):
            verify_request(
                "GET", "/internal/v1/query", body, headers,
                secret=secret, now=1_001, max_age_seconds=60,
            )
        with self.assertRaises(LightRAGAuthenticationError):
            verify_request(
                "POST", "/internal/v1/index", body, headers,
                secret=secret, now=1_001, max_age_seconds=60,
            )
        with self.assertRaises(LightRAGAuthenticationError):
            verify_request(
                "POST", "/internal/v1/query", body, headers,
                secret=secret, now=1_061, max_age_seconds=60,
            )

    def test_required_distributed_nonce_fails_closed_when_unconfigured(self):
        env = {
            "LIGHTRAG_REQUIRE_DISTRIBUTED_NONCE": "true",
            "LIGHTRAG_NONCE_REDIS_REST_URL": "",
            "LIGHTRAG_NONCE_REDIS_REST_TOKEN": "",
            "UPSTASH_REDIS_REST_URL": "",
            "UPSTASH_REDIS_REST_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(LightRAGConfigurationError):
                NonceReplayCache().consume("nonce-value-12345", expires_at=160, now=100)


if __name__ == "__main__":
    unittest.main()
