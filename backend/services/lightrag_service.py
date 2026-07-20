"""Stable product-facing facade for the optional LightRAG engine.

This module is the only LightRAG entrypoint that existing routers/services
should use.  It deliberately never falls back to the legacy engine: callers
must surface an explicit availability error or offer a user-driven switch.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from lightrag_integration.adapters import (
    LightRAGAdapter,
    LocalLightRAGAdapter,
    RemoteLightRAGAdapter,
    installed_lightrag_version,
    lightrag_package_available,
)
from lightrag_integration.errors import (
    LightRAGConfigurationError,
    LightRAGDisabledError,
    LightRAGError,
    LightRAGProtocolError,
    LightRAGUnavailableError,
    LightRAGValidationError,
)
from lightrag_integration.security import workspace_key as _derive_workspace_key
from lightrag_integration.types import Engine, LightRAGMode, TARGET_LIGHTRAG_VERSION


_adapter: LightRAGAdapter | None = None
_adapter_signature: tuple[str, str, str] | None = None
_adapter_override: LightRAGAdapter | None = None


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return _env_flag("LIGHTRAG_ENABLED", False)


def workspace_key(tenant_id: str) -> str:
    """Map a trusted tenant id to an irreversible workspace identifier."""

    return _derive_workspace_key(tenant_id)


def _transport() -> str:
    transport = os.getenv("LIGHTRAG_TRANSPORT", "remote").strip().lower()
    if transport not in {"remote", "local"}:
        raise LightRAGConfigurationError("LIGHTRAG_TRANSPORT must be remote or local")
    return transport


def _get_adapter() -> LightRAGAdapter:
    global _adapter, _adapter_signature
    if _adapter_override is not None:
        return _adapter_override
    transport = _transport()
    base_url = os.getenv("LIGHTRAG_BASE_URL", "").strip()
    secret_fingerprint = hashlib.sha256(os.getenv("LIGHTRAG_HMAC_SECRET", "").encode("utf-8")).hexdigest()[:12]
    signature = (transport, base_url, secret_fingerprint)
    if _adapter is None or _adapter_signature != signature:
        _adapter = RemoteLightRAGAdapter() if transport == "remote" else LocalLightRAGAdapter()
        _adapter_signature = signature
    return _adapter


def _require_enabled() -> LightRAGAdapter:
    if not enabled():
        raise LightRAGDisabledError("LightRAG is disabled")
    # Derive a harmless sentinel to fail fast if the workspace secret is absent.
    workspace_key("configuration-check")
    return _get_adapter()


def _configured_secret(name: str) -> bool:
    return len(os.getenv(name, "").strip().encode("utf-8")) >= 32


def _default_mode() -> LightRAGMode:
    return LightRAGMode.parse(os.getenv("LIGHTRAG_DEFAULT_MODE", "mix"))


def _max_graph_nodes(value: int | None = None) -> int:
    if value is None:
        try:
            value = int(os.getenv("LIGHTRAG_MAX_GRAPH_NODES", "200"))
        except ValueError:
            value = 200
    return max(1, min(int(value), 10000))


def _max_graph_edges(value: int | None = None) -> int:
    if value is None:
        try:
            value = int(os.getenv("LIGHTRAG_MAX_GRAPH_EDGES", "2000"))
        except ValueError:
            value = 2000
    return max(0, min(int(value), 100000))


async def health(*, probe: bool = False) -> dict[str, Any]:
    """Return a secret-free readiness profile, optionally probing Railway."""

    is_enabled = enabled()
    try:
        transport = _transport()
        default_mode = _default_mode().value
        transport_valid = True
    except LightRAGError:
        transport = os.getenv("LIGHTRAG_TRANSPORT", "remote").strip().lower()
        default_mode = os.getenv("LIGHTRAG_DEFAULT_MODE", "mix").strip().lower()
        transport_valid = False
    workspace_secret = _configured_secret("LIGHTRAG_WORKSPACE_SECRET")
    hmac_secret = _configured_secret("LIGHTRAG_HMAC_SECRET")
    base_url = bool(os.getenv("LIGHTRAG_BASE_URL", "").strip())
    transport_configured = (transport == "local") or (base_url and hmac_secret)
    configured = workspace_secret and transport_valid and transport_configured
    profile: dict[str, Any] = {
        "enabled": is_enabled,
        "configured": configured,
        "status": "disabled" if not is_enabled else "configured" if configured else "misconfigured",
        "transport": transport,
        "target_version": TARGET_LIGHTRAG_VERSION,
        "default_mode": default_mode,
        "workspace_secret_configured": workspace_secret,
        "hmac_configured": hmac_secret if transport == "remote" else None,
        "service_configured": base_url if transport == "remote" else True,
        "max_graph_nodes": _max_graph_nodes(),
        "max_graph_edges": _max_graph_edges(),
        "package_available": lightrag_package_available() if transport == "local" else None,
        "installed_version": installed_lightrag_version() if transport == "local" else None,
    }
    if not is_enabled or not configured or not probe:
        return profile
    try:
        remote = await _get_adapter().health()
    except LightRAGError:
        profile["status"] = "unavailable"
        profile["ready"] = False
        return profile
    profile["probe"] = remote
    if isinstance(remote.get("components"), dict):
        # System settings consumes named components at the facade level.
        # Preserve the complete signed response under ``probe`` as well.
        profile["components"] = remote["components"]
    if "queue_depth" in remote:
        profile["queue_depth"] = remote["queue_depth"]
    if "metrics" in remote:
        profile["metrics"] = remote["metrics"]
    profile["ready"] = remote.get("status") in {"ok", "ready", "healthy"}
    profile["status"] = "ready" if profile["ready"] else "degraded"
    return profile


def _workspaces(tenant_id: str, additional_tenants: Sequence[str] | None) -> list[str]:
    if additional_tenants and len(additional_tenants) > 8:
        raise LightRAGValidationError("at most eight additional knowledge spaces are allowed")
    tenant_ids = [tenant_id, *(additional_tenants or [])]
    result: list[str] = []
    for item in tenant_ids:
        derived = workspace_key(str(item))
        if derived not in result:
            result.append(derived)
    return result


async def index_pages(
    *,
    tenant_id: str,
    doc_id: str,
    filename: str,
    pages: Sequence[Any],
) -> dict[str, Any]:
    adapter = _require_enabled()
    return await adapter.index_pages(
        workspace=workspace_key(tenant_id),
        doc_id=str(doc_id),
        filename=str(filename),
        pages=pages,
    )


async def delete_document(
    *,
    tenant_id: str,
    doc_id: str,
    page_count: int | None = None,
    page_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    adapter = _require_enabled()
    result = await adapter.delete_document(
        workspace=workspace_key(tenant_id),
        doc_id=str(doc_id),
        page_count=page_count,
        page_ids=page_ids,
    )
    failed_page_ids = [
        str(item)
        for item in (result.get("failed_page_ids") or [])
        if str(item)
    ]
    if result.get("deleted") is not True or failed_page_ids:
        # Partial deletes are operational failures, not successful tombstones.
        # The synchronous Worker translates this typed failure into its durable
        # retry state instead of silently acknowledging the queue receipt.
        raise LightRAGUnavailableError("LightRAG deletion is incomplete and must be retried")
    return result


def _allowed(allowed_doc_ids: set[str] | Sequence[str] | None) -> set[str] | None:
    if allowed_doc_ids is None:
        return None
    return {str(item) for item in allowed_doc_ids if str(item)}


def _empty_query_result(mode: LightRAGMode) -> dict[str, Any]:
    return {
        "engine": Engine.LIGHTRAG.value,
        "retrieval_mode": mode.value,
        "answer": "",
        "references": [],
        "cited_entities": [],
        "usage": {},
        "model": "",
        "elapsed_seconds": 0.0,
        "workspace_scope_count": 0,
    }


async def _consume_scoped_query(
    adapter: LightRAGAdapter,
    *,
    workspaces: Sequence[str],
    question: str,
    mode: LightRAGMode,
    history: Sequence[Mapping[str, Any]],
    allowed_doc_ids: set[str] | None,
    include_references: bool,
) -> dict[str, Any]:
    stream_scopes = getattr(adapter, "stream_query_scopes", None)
    if not callable(stream_scopes):
        raise LightRAGProtocolError(
            "LightRAG adapter does not support safe multi-workspace queries"
        )
    completed: dict[str, Any] | None = None
    async for event in stream_scopes(
        workspaces=workspaces,
        question=question,
        mode=mode,
        history=history,
        allowed_doc_ids=allowed_doc_ids,
        include_references=include_references,
    ):
        event_name = str(event.get("event") or "")
        data = event.get("data")
        if event_name == "answer_delta":
            if not isinstance(data, Mapping):
                raise LightRAGProtocolError(
                    "LightRAG answer delta must be an object"
                )
        elif event_name == "done":
            if completed is not None or not isinstance(data, Mapping):
                raise LightRAGProtocolError(
                    "LightRAG scoped query must emit exactly one final object"
                )
            completed = dict(data)
        elif event_name == "error":
            raise LightRAGUnavailableError("LightRAG streaming query failed")
    if completed is None:
        raise LightRAGProtocolError(
            "LightRAG scoped stream ended without final metadata"
        )
    completed["workspace_scope_count"] = len(workspaces)
    return completed


async def run_query(
    *,
    tenant_id: str,
    question: str,
    mode: LightRAGMode | str | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
    allowed_doc_ids: set[str] | Sequence[str] | None = None,
    include_references: bool = True,
    additional_tenants: Sequence[str] | None = None,
) -> dict[str, Any]:
    adapter = _require_enabled()
    question = str(question or "").strip()
    if not question:
        raise LightRAGValidationError("question is required")
    mode_value = LightRAGMode.parse(mode, default=_default_mode().value)
    allowed = _allowed(allowed_doc_ids)
    if allowed is not None and not allowed:
        return _empty_query_result(mode_value)
    workspaces = _workspaces(tenant_id, additional_tenants)
    if len(workspaces) == 1:
        return await adapter.run_query(
            workspace=workspaces[0],
            question=question,
            mode=mode_value,
            history=history or (),
            allowed_doc_ids=allowed,
            include_references=include_references,
        )
    return await _consume_scoped_query(
        adapter,
        workspaces=workspaces,
        question=question,
        mode=mode_value,
        history=history or (),
        allowed_doc_ids=allowed,
        include_references=include_references,
    )


async def stream_query(
    *,
    tenant_id: str,
    question: str,
    mode: LightRAGMode | str | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
    allowed_doc_ids: set[str] | Sequence[str] | None = None,
    include_references: bool = True,
    additional_tenants: Sequence[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Retrieve isolated workspaces and stream one synthesized answer."""

    adapter = _require_enabled()
    question = str(question or "").strip()
    if not question:
        raise LightRAGValidationError("question is required")
    mode_value = LightRAGMode.parse(mode, default=_default_mode().value)
    allowed = _allowed(allowed_doc_ids)
    if allowed is not None and not allowed:
        yield {
            "event": "done",
            "data": _empty_query_result(mode_value),
        }
        return

    normalized_history = list(history or ())[-8:]
    workspaces = _workspaces(tenant_id, additional_tenants)
    if len(workspaces) > 1:
        stream_scopes = getattr(adapter, "stream_query_scopes", None)
        if not callable(stream_scopes):
            raise LightRAGProtocolError(
                "LightRAG adapter does not support safe multi-workspace queries"
            )
        source = stream_scopes(
            workspaces=workspaces,
            question=question,
            mode=mode_value,
            history=normalized_history,
            allowed_doc_ids=allowed,
            include_references=include_references,
        )
    else:
        source = adapter.stream_query(
            workspace=workspaces[0],
            question=question,
            mode=mode_value,
            history=normalized_history,
            allowed_doc_ids=allowed,
            include_references=include_references,
        )

    completed = False
    async for event in source:
        event_name = str(event.get("event") or "")
        data = event.get("data")
        if event_name == "answer_delta":
            if not isinstance(data, Mapping):
                raise LightRAGProtocolError(
                    "LightRAG answer delta must be an object"
                )
            text = str(data.get("text") or "")
            if text:
                yield {"event": "answer_delta", "data": {"text": text}}
        elif event_name == "done":
            if completed or not isinstance(data, Mapping):
                raise LightRAGProtocolError(
                    "LightRAG stream must emit exactly one final object"
                )
            completed = True
            final = dict(data)
            if len(workspaces) > 1:
                final["workspace_scope_count"] = len(workspaces)
            yield {"event": "done", "data": final}
        elif event_name == "error":
            raise LightRAGUnavailableError("LightRAG streaming query failed")
    if not completed:
        raise LightRAGProtocolError("LightRAG stream ended without final metadata")


def _merge_graphs(results: list[dict[str, Any]], *, doc_id: str | None, max_nodes: int, max_edges: int) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    edge_ids: set[str] = set()
    truncated = False
    for result in results:
        truncated = truncated or bool(result.get("truncated"))
        for node in result.get("nodes", []):
            if node.get("id") not in node_ids and len(nodes) < max_nodes:
                node_ids.add(node.get("id"))
                nodes.append(node)
        for edge in result.get("edges", []):
            if (
                edge.get("id") not in edge_ids
                and edge.get("source") in node_ids
                and edge.get("target") in node_ids
                and len(edges) < max_edges
            ):
                edge_ids.add(edge.get("id"))
                edges.append(edge)
    return {
        "format": "json",
        "engine": Engine.LIGHTRAG.value,
        "doc_id": doc_id,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated or any(len(result.get("nodes", [])) > len(nodes) for result in results),
        "workspace_scope_count": len(results),
    }


async def export_graph(
    *,
    tenant_id: str,
    doc_id: str | None = None,
    allowed_doc_ids: set[str] | Sequence[str] | None = None,
    max_nodes: int | None = None,
    max_edges: int | None = None,
    additional_tenants: Sequence[str] | None = None,
) -> dict[str, Any]:
    adapter = _require_enabled()
    allowed = _allowed(allowed_doc_ids)
    node_limit, edge_limit = _max_graph_nodes(max_nodes), _max_graph_edges(max_edges)
    if allowed is not None and not allowed:
        return _merge_graphs([], doc_id=doc_id, max_nodes=node_limit, max_edges=edge_limit)
    results = await asyncio.gather(*(
        adapter.export_graph(
            workspace=workspace,
            doc_id=doc_id,
            allowed_doc_ids=allowed,
            max_nodes=node_limit,
            max_edges=edge_limit,
        )
        for workspace in _workspaces(tenant_id, additional_tenants)
    ))
    return _merge_graphs(list(results), doc_id=doc_id, max_nodes=node_limit, max_edges=edge_limit)


async def search_entities(
    *,
    tenant_id: str,
    query: str,
    entity_type: str | None = None,
    limit: int = 15,
    allowed_doc_ids: set[str] | Sequence[str] | None = None,
    additional_tenants: Sequence[str] | None = None,
) -> dict[str, Any]:
    adapter = _require_enabled()
    query = str(query or "").strip()
    if not query:
        raise LightRAGValidationError("query is required")
    limit = max(1, min(int(limit), 100))
    allowed = _allowed(allowed_doc_ids)
    if allowed is not None and not allowed:
        return {"engine": Engine.LIGHTRAG.value, "query": query, "total": 0, "items": []}
    results = await asyncio.gather(*(
        adapter.search_entities(
            workspace=workspace,
            query=query,
            entity_type=entity_type,
            limit=limit,
            allowed_doc_ids=allowed,
        )
        for workspace in _workspaces(tenant_id, additional_tenants)
    ))
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        for item in result.get("items", []):
            item_id = str(item.get("id") or "")
            if item_id and item_id not in seen and len(items) < limit:
                seen.add(item_id)
                items.append(item)
    return {
        "engine": Engine.LIGHTRAG.value,
        "query": query,
        "total": len(items),
        "items": items,
        "workspace_scope_count": len(results),
    }


def _set_adapter_for_tests(adapter: LightRAGAdapter | None) -> None:
    """Test seam; production code should rely on environment configuration."""

    global _adapter_override, _adapter, _adapter_signature
    _adapter_override = adapter
    _adapter = None
    _adapter_signature = None


__all__ = [
    "Engine",
    "LightRAGMode",
    "LightRAGError",
    "LightRAGDisabledError",
    "LightRAGConfigurationError",
    "LightRAGUnavailableError",
    "enabled",
    "health",
    "workspace_key",
    "index_pages",
    "delete_document",
    "run_query",
    "export_graph",
    "search_entities",
]
