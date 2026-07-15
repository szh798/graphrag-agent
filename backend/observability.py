"""Lightweight request tracing and structured access logs.

The edge proxy forwards ``X-Request-ID`` when it has one. Direct backend
requests receive a generated id so every response can be correlated with a
single JSON access-log entry without logging request bodies or credentials.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar, Token

from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger("graphrag.access")
_current_request_id: ContextVar[str | None] = ContextVar("graphrag_request_id", default=None)


def get_request_id() -> str:
    """Return the request-scoped correlation id, generating one off-request."""
    return _current_request_id.get() or str(uuid.uuid4())


def _request_id(scope: Scope) -> str:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == b"x-request-id":
            value = raw_value.decode("latin-1").strip()
            if value:
                return value[:128]
    return str(uuid.uuid4())


class RequestContextMiddleware:
    """Attach a request id and emit one privacy-safe structured access log."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id(scope)
        context_token: Token = _current_request_id.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_context(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = list(message.get("headers", []))
                if not any(name.lower() == b"x-request-id" for name, _ in headers):
                    headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            logger.info(
                json.dumps(
                    {
                        "event": "http_request",
                        "request_id": request_id,
                        "method": scope.get("method", ""),
                        "path": scope.get("path", ""),
                        "status": status_code,
                        "duration_ms": elapsed_ms,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            _current_request_id.reset(context_token)
