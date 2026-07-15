"""Clerk-backed request identity with anonymous visitor fallback.

The browser token is verified against Clerk's JWKS.  Anonymous public-demo
requests keep the existing visitor UUID boundary, while authenticated users
are scoped to their active Clerk Organization (or a personal tenant).
"""
from __future__ import annotations

import base64
import ipaddress
import os
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, Request


VISITOR_ID_HEADER = "X-GraphRAG-Visitor-ID"
_ADMIN_ROLES = {"admin", "owner"}


@dataclass(frozen=True)
class RequestIdentity:
    authenticated: bool
    actor_id: str
    tenant_id: str
    role: str
    visitor_id: str | None = None
    organization_id: str | None = None
    organization_slug: str | None = None
    session_id: str | None = None
    permissions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def owner_id(self) -> str:
        return self.tenant_id

    @property
    def is_admin(self) -> bool:
        return self.role in _ADMIN_ROLES

    def public_dict(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "user_id": self.actor_id if self.authenticated else None,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "organization_slug": self.organization_slug,
            "role": self.role,
            "permissions": list(self.permissions),
        }


def _canonical_visitor(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw or raw != raw.lower():
        return None
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None
    if str(parsed) != raw or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        return None
    return raw


def _frontend_api_from_publishable_key() -> str:
    raw = os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", os.getenv("CLERK_PUBLISHABLE_KEY", "")).strip().strip("\"'")
    for prefix in ("pk_test_", "pk_live_"):
        if raw.startswith(prefix):
            encoded = raw[len(prefix):]
            padding = "=" * (-len(encoded) % 4)
            try:
                decoded = base64.urlsafe_b64decode(encoded + padding).decode("utf-8", "ignore").rstrip("$")
            except (ValueError, UnicodeDecodeError):
                return ""
            return decoded if decoded.endswith(".clerk.accounts.dev") else ""
    return ""


def clerk_issuer() -> str:
    configured = os.getenv("CLERK_ISSUER", "").strip().rstrip("/")
    if configured:
        return configured
    frontend_api = _frontend_api_from_publishable_key()
    return f"https://{frontend_api}" if frontend_api else ""


def clerk_jwks_url() -> str:
    configured = os.getenv("CLERK_JWKS_URL", "").strip()
    if configured:
        return configured
    issuer = clerk_issuer()
    return f"{issuer}/.well-known/jwks.json" if issuer else ""


def _authorized_parties() -> set[str]:
    raw = os.getenv(
        "CLERK_AUTHORIZED_PARTIES",
        "https://graphrag-studio.opc249255.chatgpt.site,http://localhost:5173,http://127.0.0.1:5173",
    )
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


@lru_cache(maxsize=4)
def _jwks_client(url: str):
    if not url:
        raise RuntimeError("Clerk JWKS URL is not configured")
    return jwt.PyJWKClient(url, cache_keys=True, timeout=5)


def _verified_claims(token: str) -> dict[str, Any]:
    jwks_url = clerk_jwks_url()
    issuer = clerk_issuer()
    if not jwks_url or not issuer:
        raise HTTPException(status_code=503, detail="Account authentication is not configured")
    try:
        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
        kwargs: dict[str, Any] = {
            "algorithms": ["RS256"],
            "issuer": issuer,
            "options": {"require": ["exp", "iat", "sub"]},
            "leeway": 5,
        }
        audience = os.getenv("CLERK_AUDIENCE", "").strip()
        if audience:
            kwargs["audience"] = audience
        else:
            kwargs["options"]["verify_aud"] = False
        claims = jwt.decode(token, signing_key.key, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired account session") from exc

    azp = str(claims.get("azp") or "").rstrip("/")
    allowed = _authorized_parties()
    if azp and allowed and azp not in allowed:
        raise HTTPException(status_code=401, detail="Account session origin is not allowed")
    if claims.get("sts") == "pending":
        raise HTTPException(status_code=403, detail="Select or create an organization before continuing")
    return claims


def _organization_claims(claims: dict[str, Any]) -> tuple[str | None, str | None, str, tuple[str, ...]]:
    compact = claims.get("o") if isinstance(claims.get("o"), dict) else {}
    org_id = str(compact.get("id") or claims.get("org_id") or "").strip() or None
    org_slug = str(compact.get("slg") or claims.get("org_slug") or "").strip() or None
    role = str(compact.get("rol") or claims.get("org_role") or "member").strip().lower()
    if role.startswith("org:"):
        role = role.split(":", 1)[1]
    raw_permissions = compact.get("per") or claims.get("org_permissions") or []
    if isinstance(raw_permissions, str):
        permissions = tuple(item.strip() for item in raw_permissions.split(",") if item.strip())
    elif isinstance(raw_permissions, list):
        permissions = tuple(str(item) for item in raw_permissions)
    else:
        permissions = ()
    return org_id, org_slug, role or "member", permissions


def _allow_legacy_local_identity(request: Request) -> bool:
    environment = (
        os.getenv("GRAPHRAG_ENV")
        or os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or ""
    ).strip().lower()
    if os.getenv("VERCEL") in {"1", "true", "True"} or environment in {"prod", "production"}:
        return False
    if environment in {"local", "dev", "development", "test", "testing"} or os.getenv("PYTEST_CURRENT_TEST"):
        return True
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in {"localhost", "testclient"}


def resolve_identity(
    authorization: str | None,
    visitor_id: str | None,
    *,
    allow_legacy: bool = False,
) -> RequestIdentity:
    canonical_visitor = _canonical_visitor(visitor_id)
    raw_auth = (authorization or "").strip()
    if not raw_auth:
        if not canonical_visitor and allow_legacy:
            return RequestIdentity(
                authenticated=False,
                actor_id="default",
                tenant_id="default",
                role="visitor",
            )
        if not canonical_visitor:
            raise HTTPException(status_code=400, detail=f"A valid {VISITOR_ID_HEADER} header is required")
        return RequestIdentity(
            authenticated=False,
            actor_id=canonical_visitor,
            tenant_id=canonical_visitor,
            visitor_id=canonical_visitor,
            role="visitor",
        )
    if not raw_auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Unsupported account authorization scheme")

    claims = _verified_claims(raw_auth.split(" ", 1)[1].strip())
    user_id = str(claims["sub"])
    org_id, org_slug, role, permissions = _organization_claims(claims)
    tenant_id = org_id or f"user:{user_id}"
    return RequestIdentity(
        authenticated=True,
        actor_id=user_id,
        tenant_id=tenant_id,
        organization_id=org_id,
        organization_slug=org_slug,
        session_id=str(claims.get("sid") or "") or None,
        role=role if org_id else "owner",
        permissions=permissions,
        visitor_id=canonical_visitor,
    )


async def get_request_identity(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    visitor_id: str | None = Header(default=None, alias=VISITOR_ID_HEADER),
) -> RequestIdentity:
    identity = resolve_identity(
        authorization,
        visitor_id,
        allow_legacy=_allow_legacy_local_identity(request),
    )
    request.state.identity = identity
    return identity


async def require_authenticated_identity(
    identity: RequestIdentity = Depends(get_request_identity),
) -> RequestIdentity:
    if not identity.authenticated:
        raise HTTPException(status_code=401, detail="Sign in to use account features")
    return identity


def require_admin(identity: RequestIdentity) -> None:
    if not identity.authenticated:
        raise HTTPException(status_code=401, detail="Sign in to continue")
    if not identity.is_admin:
        raise HTTPException(status_code=403, detail="Organization administrator permission required")
