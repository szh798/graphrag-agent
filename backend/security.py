"""HTTP perimeter controls for requests forwarded by the trusted site proxy."""
from __future__ import annotations

import hmac
import os
import uuid

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


PROXY_SECRET_HEADER = "X-GraphRAG-Proxy-Secret"
_API_PREFIX = "/api/v1"
_PUBLIC_API_PATHS = {f"{_API_PREFIX}/health/live"}
_TRUE_VALUES = {"1", "true", "yes", "on"}


def env_flag(name: str, default: bool = False) -> bool:
    """Read a conventional boolean environment flag."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _is_protected_api_path(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    is_api_path = normalized == _API_PREFIX or normalized.startswith(f"{_API_PREFIX}/")
    return is_api_path and normalized not in _PUBLIC_API_PATHS


def _error_response(status_code: int, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "msg": message,
            "request_id": str(uuid.uuid4()),
            "data": None,
        },
    )


class ProxyAuthMiddleware:
    """Require a shared secret on protected API traffic from the site proxy.

    Authentication is enabled whenever ``BACKEND_PROXY_SECRET`` is non-empty,
    or explicitly required with ``REQUIRE_PROXY_AUTH=true``. If authentication
    is required but no secret is configured, protected routes fail closed.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        secret: str | None = None,
        require_auth: bool | None = None,
    ) -> None:
        self.app = app
        configured_secret = os.getenv("BACKEND_PROXY_SECRET", "") if secret is None else secret
        self.secret = configured_secret.strip()
        self.require_auth = env_flag("REQUIRE_PROXY_AUTH") if require_auth is None else require_auth
        self.enabled = bool(self.secret) or self.require_auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_protected_api_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        if not self.enabled:
            await self.app(scope, receive, send)
            return

        if not self.secret:
            response = _error_response(503, 5001, "Service unavailable")
            await response(scope, receive, send)
            return

        supplied_secret = Headers(scope=scope).get(PROXY_SECRET_HEADER, "")
        if not supplied_secret or not hmac.compare_digest(supplied_secret, self.secret):
            response = _error_response(401, 4001, "Unauthorized")
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
