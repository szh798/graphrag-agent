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

from identity import resolve_identity
from models.schemas import APIResponse
from observability import RequestContextMiddleware


VISITOR_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class IdentityTests(unittest.TestCase):
    def test_anonymous_identity_keeps_canonical_visitor_tenant(self):
        identity = resolve_identity(None, VISITOR_ID)

        self.assertFalse(identity.authenticated)
        self.assertEqual(identity.actor_id, VISITOR_ID)
        self.assertEqual(identity.tenant_id, VISITOR_ID)
        self.assertEqual(identity.role, "visitor")

    def test_clerk_v2_organization_claim_becomes_admin_tenant(self):
        claims = {
            "sub": "user_123",
            "sid": "sess_123",
            "o": {"id": "org_123", "slg": "research", "rol": "admin", "per": ["org:sys_memberships:manage"]},
        }
        with patch("identity._verified_claims", return_value=claims):
            identity = resolve_identity("Bearer signed-session", VISITOR_ID)

        self.assertTrue(identity.authenticated)
        self.assertEqual(identity.actor_id, "user_123")
        self.assertEqual(identity.tenant_id, "org_123")
        self.assertEqual(identity.organization_slug, "research")
        self.assertTrue(identity.is_admin)
        self.assertEqual(identity.permissions, ("org:sys_memberships:manage",))

    def test_authenticated_user_without_org_gets_personal_tenant(self):
        with patch("identity._verified_claims", return_value={"sub": "user_456", "sid": "sess_456"}):
            identity = resolve_identity("Bearer signed-session", None)

        self.assertEqual(identity.tenant_id, "user:user_456")
        self.assertEqual(identity.role, "owner")
        self.assertTrue(identity.is_admin)


class RequestIdTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return APIResponse.ok({"ok": True})

        self.client = TestClient(app)

    def test_response_body_and_header_share_one_generated_request_id(self):
        response = self.client.get("/test")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["request_id"], response.headers["x-request-id"])

    def test_valid_incoming_request_id_is_preserved_end_to_end(self):
        response = self.client.get("/test", headers={"X-Request-ID": "edge-request-123"})

        self.assertEqual(response.json()["request_id"], "edge-request-123")
        self.assertEqual(response.headers["x-request-id"], "edge-request-123")


if __name__ == "__main__":
    unittest.main()
