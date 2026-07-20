"""Small, dependency-free HTTP helpers for LightRAG operations scripts."""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    """An operations API call failed without exposing request credentials."""


def join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def auth_headers_from_env() -> dict[str, str]:
    """Build an optional auth header without ever returning it in reports."""
    header_name = os.getenv("LIGHTRAG_OPS_AUTH_HEADER", "Authorization").strip()
    token = os.getenv("LIGHTRAG_OPS_AUTH_TOKEN", "").strip()
    if not token:
        return {}
    if header_name.lower() == "authorization" and not token.lower().startswith("bearer "):
        token = f"Bearer {token}"
    return {header_name: token}


def unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload and "code" in payload:
        if payload.get("code") not in (0, "0", None):
            raise ApiError(str(payload.get("msg") or "API returned an error"))
        return payload.get("data")
    return payload


class JsonHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.headers = {"Accept": "application/json", **(headers or {})}

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = join_url(self.base_url, path)
        if query:
            url = f"{url}?{urlencode(query)}"
        body = None
        headers = dict(self.headers)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            # Do not copy an upstream response body into terminal/CI logs. It
            # may contain a question, filename, citation, or internal detail.
            exc.read(2048)
            raise ApiError(f"{method.upper()} {path} returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise ApiError(f"{method.upper()} {path} failed: {exc.reason}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(f"{method.upper()} {path} returned invalid JSON") from exc
