"""Normalize changing LightRAG response shapes into the Studio contract."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .types import Engine, LightRAGMode, parse_source_path


_FIELD_SEPARATORS = re.compile(r"(?:<SEP>|\|\|\||\x1f)")


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    if hasattr(value, "__dict__"):
        return {key: val for key, val in vars(value).items() if not key.startswith("_")}
    return {}


def _items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        for key in ("items", "nodes", "edges", "references", "results"):
            if isinstance(value.get(key), list):
                return list(value[key])
    return []


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in _FIELD_SEPARATORS.split(value) if item.strip()]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _opaque_id(kind: str, workspace: str, *parts: Any) -> str:
    raw = "\x00".join((workspace, *(str(part) for part in parts)))
    return f"lightrag:{kind}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _source_records(properties: Mapping[str, Any]) -> list[dict[str, Any]]:
    paths: list[str] = []
    for key in ("file_path", "file_paths", "source_path", "source_paths"):
        paths.extend(_strings(properties.get(key)))
    records = [parse_source_path(path) for path in dict.fromkeys(paths)]
    explicit_doc = str(properties.get("doc_id") or properties.get("source_doc") or "").strip()
    explicit_page = properties.get("page")
    if not records and (explicit_doc or explicit_page is not None):
        try:
            page = int(explicit_page or 0)
        except (TypeError, ValueError):
            page = 0
        records.append({
            "doc_id": explicit_doc,
            "filename": str(properties.get("filename") or "document"),
            "page": page,
            "file_path": "",
        })
    return records


def normalize_references(raw: Any, *, allowed_doc_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Return stable page-level references without leaking workspace details."""

    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for position, item in enumerate(_items(raw), start=1):
        data = _mapping(item)
        reference_id = str(data.get("reference_id") or data.get("id") or position)
        source = parse_source_path(data.get("file_path") or data.get("source_path"))
        doc_id = str(data.get("doc_id") or source["doc_id"] or "")
        # A scoped query must never publish evidence whose document ownership
        # cannot be proven. Missing/invalid source metadata therefore fails
        # closed just like an explicitly denied document id.
        if allowed_doc_ids is not None and doc_id not in allowed_doc_ids:
            continue
        try:
            page = int(data.get("page") or source["page"] or 0)
        except (TypeError, ValueError):
            page = 0
        raw_content = data.get("content", data.get("excerpt", ""))
        if isinstance(raw_content, list):
            excerpt = "\n\n".join(str(part) for part in raw_content if part)
        else:
            excerpt = str(raw_content or "")
        chunk_id = str(data.get("chunk_id") or data.get("source_id") or reference_id)
        key = (doc_id, page, chunk_id)
        if key in seen:
            continue
        seen.add(key)
        refs.append({
            "reference_id": reference_id,
            "doc_id": doc_id,
            "filename": str(data.get("filename") or source["filename"] or "document"),
            "page": page,
            "chunk_id": chunk_id,
            "excerpt": excerpt[:2000],
            "file_path": source["file_path"],
        })
    return refs


def _query_payload(raw: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if isinstance(raw, str):
        return raw, {}, {}
    root = _mapping(raw)
    data = _mapping(root.get("data"))
    llm_response = _mapping(root.get("llm_response"))
    answer = str(
        root.get("answer")
        or root.get("response")
        or llm_response.get("content")
        or data.get("response")
        or ""
    )
    metadata = _mapping(root.get("metadata"))
    if root.get("response_time") is not None:
        metadata.setdefault("response_time", root["response_time"])
    return answer, data, metadata


def normalize_query_result(
    raw: Any,
    *,
    workspace: str,
    mode: LightRAGMode | str,
    allowed_doc_ids: set[str] | None = None,
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    mode_value = LightRAGMode.parse(mode)
    answer, data, metadata = _query_payload(raw)
    root = _mapping(raw)
    raw_refs = data.get("references", root.get("references", []))
    references = normalize_references(raw_refs, allowed_doc_ids=allowed_doc_ids)
    cited_entities: list[str] = []
    for item in _items(data.get("entities", [])):
        entity = _mapping(item)
        properties = {
            **_mapping(entity.get("properties")),
            **{key: value for key, value in entity.items() if key != "properties"},
        }
        if allowed_doc_ids is not None:
            entity_doc_ids, _, _, _ = _node_sources(properties)
            if not entity_doc_ids or not allowed_doc_ids.intersection(
                entity_doc_ids
            ):
                continue
        name = str(entity.get("entity_name") or entity.get("name") or entity.get("id") or item).strip()
        if name and name not in cited_entities:
            cited_entities.append(name)
    usage = _mapping(metadata.get("usage", root.get("usage", {})))
    model_name = str(metadata.get("model") or root.get("model") or "").strip()
    return {
        "engine": Engine.LIGHTRAG.value,
        "retrieval_mode": mode_value.value,
        "answer": answer,
        "references": references,
        "cited_entities": cited_entities,
        "usage": usage,
        "model": model_name,
        "elapsed_seconds": float(elapsed_seconds if elapsed_seconds is not None else metadata.get("response_time") or 0.0),
        # A non-sensitive correlation key helps the facade merge isolated calls.
        "workspace_fingerprint": workspace[-8:],
    }


def _node_sources(properties: Mapping[str, Any]) -> tuple[list[str], list[int], str, str]:
    records = _source_records(properties)
    doc_ids = list(dict.fromkeys(str(item["doc_id"]) for item in records if item.get("doc_id")))
    pages = sorted({int(item["page"]) for item in records if int(item.get("page") or 0) > 0})
    filename = next((str(item["filename"]) for item in records if item.get("filename")), "document")
    source_doc = doc_ids[0] if doc_ids else str(properties.get("source_doc") or properties.get("doc_id") or "")
    return doc_ids, pages, filename, source_doc


def normalize_graph(
    raw: Any,
    *,
    workspace: str,
    doc_id: str | None = None,
    allowed_doc_ids: set[str] | None = None,
    max_nodes: int = 200,
    max_edges: int = 2000,
) -> dict[str, Any]:
    root = _mapping(raw)
    raw_nodes = _items(root.get("nodes", raw if isinstance(raw, list) else []))
    raw_edges = _items(root.get("edges", root.get("relationships", [])))
    nodes: list[dict[str, Any]] = []
    raw_to_normal: dict[str, str] = {}
    for item in raw_nodes:
        data = _mapping(item)
        properties = {**_mapping(data.get("properties")), **{key: val for key, val in data.items() if key != "properties"}}
        raw_id = str(
            data.get("id")
            or properties.get("entity_id")
            or properties.get("entity_name")
            or properties.get("name")
            or data.get("label")
            or ""
        ).strip()
        if not raw_id:
            continue
        doc_ids, pages, filename, source_doc = _node_sources(properties)
        if doc_id and doc_id not in doc_ids and source_doc != doc_id:
            continue
        if allowed_doc_ids is not None and (
            not doc_ids or not allowed_doc_ids.intersection(doc_ids)
        ):
            continue
        normalized_id = _opaque_id("node", workspace, raw_id)
        raw_to_normal[raw_id] = normalized_id
        try:
            degree = int(properties.get("degree") or 0)
        except (TypeError, ValueError):
            degree = 0
        nodes.append({
            "id": normalized_id,
            "name": str(
                properties.get("entity_name")
                or properties.get("entity_id")
                or properties.get("name")
                or data.get("label")
                or raw_id
            ),
            "type": str(properties.get("entity_type") or properties.get("type") or "UNKNOWN").upper(),
            "description": str(properties.get("description") or ""),
            "source_doc": source_doc,
            "source_docs": doc_ids,
            "filename": filename,
            "pages": pages,
            "page": pages[0] if pages else 0,
            "degree": degree,
            "engine": Engine.LIGHTRAG.value,
        })
        if len(nodes) >= max(1, max_nodes):
            break
    edges: list[dict[str, Any]] = []
    for item in raw_edges:
        data = _mapping(item)
        properties = {**_mapping(data.get("properties")), **{key: val for key, val in data.items() if key != "properties"}}
        raw_source = str(
            data.get("source") or data.get("src_id") or properties.get("source") or properties.get("src_id") or ""
        ).strip()
        raw_target = str(
            data.get("target") or data.get("tgt_id") or properties.get("target") or properties.get("tgt_id") or ""
        ).strip()
        source = raw_to_normal.get(raw_source)
        target = raw_to_normal.get(raw_target)
        if not source or not target:
            continue
        doc_ids, pages, _, source_doc = _node_sources(properties)
        if doc_id and doc_id not in doc_ids and source_doc != doc_id:
            continue
        if allowed_doc_ids is not None and (
            not doc_ids or not allowed_doc_ids.intersection(doc_ids)
        ):
            continue
        try:
            weight = float(properties.get("weight") or 1.0)
        except (TypeError, ValueError):
            weight = 1.0
        description = str(properties.get("description") or "")
        relation = str(properties.get("relation") or properties.get("keywords") or "RELATED_TO")
        edges.append({
            "id": _opaque_id("edge", workspace, raw_source, raw_target, relation),
            "source": source,
            "target": target,
            "relation": relation,
            "description": description,
            "weight": weight,
            "doc_id": source_doc,
            "pages": pages,
            "page": pages[0] if pages else 0,
            "engine": Engine.LIGHTRAG.value,
        })
        if len(edges) >= max(0, max_edges):
            break
    return {
        "format": "json",
        "engine": Engine.LIGHTRAG.value,
        "doc_id": doc_id,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "nodes": nodes,
        "edges": edges,
        "truncated": len(raw_nodes) > len(nodes) or len(raw_edges) > len(edges),
    }


def normalize_entity_search(
    raw: Any,
    *,
    workspace: str,
    query: str,
    entity_type: str | None,
    limit: int,
    allowed_doc_ids: set[str] | None = None,
) -> dict[str, Any]:
    if isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
        raw = {"nodes": [{"id": item, "name": item} for item in raw]}
    graph = normalize_graph(
        raw,
        workspace=workspace,
        allowed_doc_ids=allowed_doc_ids,
        max_nodes=max(limit * 3, limit),
        max_edges=0,
    )
    needle = query.casefold().strip()
    type_filter = str(entity_type or "").upper()
    items = [
        node for node in graph["nodes"]
        if (not needle or needle in node["name"].casefold())
        and (not type_filter or node["type"] == type_filter)
    ][:limit]
    return {"engine": Engine.LIGHTRAG.value, "query": query, "total": len(items), "items": items}
