"""KG Service — graph operations through the active graph repository."""
from __future__ import annotations

import os
from collections import deque

from storage import graph_repository as graph_store
from pipeline.cooccurrence import select_sparse_layout_edges


def _allowed_export(allowed_doc_ids: set[str]) -> dict:
    repo = graph_store.get_graph_repository()
    nodes: list[dict] = []
    edges: list[dict] = []
    for doc_id in sorted(allowed_doc_ids):
        exported = repo.export_kg(doc_id)
        nodes.extend(exported.get("nodes", []))
        edges.extend(exported.get("edges", []))
    node_map = {node.get("id"): node for node in nodes if node.get("id")}
    edge_map = {
        (edge.get("id") or f"{edge.get('source')}:{edge.get('target')}:{edge.get('relation')}"): edge
        for edge in edges
        if edge.get("source") in node_map and edge.get("target") in node_map
    }
    return {"nodes": list(node_map.values()), "edges": list(edge_map.values())}


def get_nodes(page: int = 1, page_size: int = 50,
              node_type: str | None = None,
              doc_id: str | None = None,
              confidence: str | None = None) -> dict:
    return graph_store.get_graph_repository().get_nodes(page, page_size, node_type, doc_id, confidence)


def get_edges(page: int = 1, page_size: int = 100,
              doc_id: str | None = None,
              relation: str | None = None) -> dict:
    return graph_store.get_graph_repository().get_edges(page, page_size, doc_id, relation)


def get_layout_edges(
    doc_id: str | None = None,
    allowed_doc_ids: set[str] | None = None,
    relation: str | None = None,
) -> dict:
    exported = export_kg(doc_id, allowed_doc_ids)
    raw_edges = exported.get("edges", [])
    items = select_sparse_layout_edges(exported.get("nodes", []), raw_edges)
    if relation:
        items = [item for item in items if item.get("relation") == relation]
    return {"items": items, "raw_total": len(raw_edges)}


def get_node_detail(node_id: str) -> dict | None:
    return graph_store.get_graph_repository().get_node_detail(node_id)


def get_neighbors(node_id: str, hops: int = 1) -> dict | None:
    return graph_store.get_graph_repository().get_neighbors(node_id, hops)


def get_stats(allowed_doc_ids: set[str] | None = None) -> dict:
    if allowed_doc_ids is not None:
        scoped = _allowed_export(allowed_doc_ids)
        type_distribution: dict[str, int] = {}
        relation_types: dict[str, int] = {}
        for node in scoped["nodes"]:
            node_type = node.get("type", "UNKNOWN")
            type_distribution[node_type] = type_distribution.get(node_type, 0) + 1
        for edge in scoped["edges"]:
            relation = edge.get("relation", "CO_OCCURS_IN")
            relation_types[relation] = relation_types.get(relation, 0) + 1
        return {
            "total_nodes": len(scoped["nodes"]),
            "total_edges": len(scoped["edges"]),
            "type_distribution": type_distribution,
            "relation_types": relation_types,
            "source_documents": sorted(allowed_doc_ids),
        }
    return graph_store.get_graph_repository().get_stats()


def export_kg(doc_id: str | None = None, allowed_doc_ids: set[str] | None = None) -> dict:
    if allowed_doc_ids is not None:
        if doc_id and doc_id not in allowed_doc_ids:
            return {"format": "json", "doc_id": doc_id, "total_nodes": 0, "total_edges": 0, "nodes": [], "edges": []}
        scoped = _allowed_export({doc_id} if doc_id else allowed_doc_ids)
        return {
            "format": "json",
            "doc_id": doc_id,
            "total_nodes": len(scoped["nodes"]),
            "total_edges": len(scoped["edges"]),
            **scoped,
        }
    return graph_store.get_graph_repository().export_kg(doc_id)


def _public_tenants(tenant_id: str, allowed_doc_ids: set[str] | None) -> list[str] | None:
    if allowed_doc_ids is None or tenant_id == "public_demo":
        return None
    public_ids = {
        item.strip()
        for item in os.getenv("PUBLIC_DOCUMENT_IDS", "").split(",")
        if item.strip()
    }
    return ["public_demo"] if allowed_doc_ids & public_ids else None


def _normalize_lightrag_graph(result: dict, doc_id: str | None = None) -> dict:
    nodes: list[dict] = []
    for raw in result.get("nodes") or []:
        node = dict(raw)
        pages = sorted({int(page) for page in (node.get("pages") or []) if page is not None})
        if not pages and node.get("page") is not None:
            pages = [int(node["page"])]
        node.update({
            "engine": "lightrag",
            "pages": pages,
            "page": pages[0] if pages else 0,
            "degree": int(node.get("degree") or 0),
            "source_doc": str(node.get("source_doc") or node.get("doc_id") or ""),
            "description": node.get("description") or "",
        })
        nodes.append(node)

    edges: list[dict] = []
    for position, raw in enumerate(result.get("edges") or []):
        edge = dict(raw)
        pages = sorted({int(page) for page in (edge.get("pages") or []) if page is not None})
        if not pages and edge.get("page") is not None:
            pages = [int(edge["page"])]
        edge.update({
            "id": str(edge.get("id") or f"lightrag:edge:{position}:{edge.get('source')}:{edge.get('target')}"),
            "engine": "lightrag",
            "pages": pages,
            "page": pages[0] if pages else 0,
            "weight": float(edge.get("weight") or 1.0),
            "description": edge.get("description") or edge.get("relation") or "",
            "doc_id": str(edge.get("doc_id") or edge.get("source_doc") or ""),
        })
        edges.append(edge)
    return {
        "format": "json",
        "doc_id": doc_id,
        "engine": "lightrag",
        "total_nodes": int(result.get("total_nodes") or len(nodes)),
        "total_edges": int(result.get("total_edges") or len(edges)),
        "nodes": nodes,
        "edges": edges,
        "truncated": bool(result.get("truncated")),
        "workspace_scope_count": int(result.get("workspace_scope_count") or 1),
    }


async def export_kg_for_engine(
    engine: str,
    *,
    tenant_id: str,
    doc_id: str | None = None,
    allowed_doc_ids: set[str] | None = None,
    complete: bool = False,
) -> dict:
    if engine == "legacy":
        result = export_kg(doc_id, allowed_doc_ids)
        result["engine"] = "legacy"
        return result
    if engine != "lightrag":
        raise ValueError("INVALID_ENGINE")

    from services import lightrag_service

    max_nodes = int(os.getenv("LIGHTRAG_EXPORT_MAX_NODES", "10000") if complete else os.getenv("LIGHTRAG_MAX_GRAPH_NODES", "200"))
    max_edges = int(os.getenv("LIGHTRAG_EXPORT_MAX_EDGES", "100000") if complete else os.getenv("LIGHTRAG_MAX_GRAPH_EDGES", "2000"))
    result = await lightrag_service.export_graph(
        tenant_id=tenant_id,
        doc_id=doc_id,
        allowed_doc_ids=allowed_doc_ids,
        max_nodes=max_nodes,
        max_edges=max_edges,
        additional_tenants=_public_tenants(tenant_id, allowed_doc_ids),
    )
    return _normalize_lightrag_graph(result, doc_id)


async def get_nodes_for_engine(
    engine: str,
    *,
    tenant_id: str,
    page: int = 1,
    page_size: int = 50,
    node_type: str | None = None,
    doc_id: str | None = None,
    confidence: str | None = None,
    source_page: int | None = None,
    allowed_doc_ids: set[str] | None = None,
    layout: bool = False,
) -> dict:
    if engine == "legacy" and allowed_doc_ids is None:
        return get_nodes(page, page_size, node_type, doc_id, confidence)
    graph = await export_kg_for_engine(
        engine,
        tenant_id=tenant_id,
        doc_id=doc_id,
        allowed_doc_ids=allowed_doc_ids,
        # The interactive layout deliberately uses the small graph window.
        # Every ordinary paginated request must see the complete business graph
        # (up to the separately configured export safety ceiling).
        complete=engine == "lightrag" and not layout,
    )
    items = graph.get("nodes", [])
    if node_type:
        items = [item for item in items if item.get("type") == node_type]
    if confidence and engine == "legacy":
        items = [item for item in items if item.get("confidence") == confidence]
    if source_page is not None:
        items = [item for item in items if source_page in (item.get("pages") or [item.get("page")])]
    total = len(items)
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items[start:start + page_size],
        "truncated": bool(graph.get("truncated")),
    }


async def get_edges_for_engine(
    engine: str,
    *,
    tenant_id: str,
    page: int = 1,
    page_size: int = 100,
    doc_id: str | None = None,
    relation: str | None = None,
    min_weight: float | None = None,
    source_page: int | None = None,
    allowed_doc_ids: set[str] | None = None,
    layout: bool = False,
) -> dict:
    if engine == "legacy" and allowed_doc_ids is None and not layout:
        return get_edges(page, page_size, doc_id, relation)
    graph = await export_kg_for_engine(
        engine,
        tenant_id=tenant_id,
        doc_id=doc_id,
        allowed_doc_ids=allowed_doc_ids,
        complete=engine == "lightrag" and not layout,
    )
    raw_items = graph.get("edges", [])
    items = select_sparse_layout_edges(graph.get("nodes", []), raw_items) if layout else raw_items
    if relation:
        items = [item for item in items if item.get("relation") == relation]
    if min_weight is not None and engine == "lightrag":
        items = [item for item in items if float(item.get("weight") or 0) >= min_weight]
    if source_page is not None:
        items = [item for item in items if source_page in (item.get("pages") or [item.get("page")])]
    total = len(items)
    start = (page - 1) * page_size
    return {
        "total": total,
        "raw_total": len(raw_items),
        "page": page,
        "page_size": page_size,
        "items": items[start:start + page_size],
        "truncated": bool(graph.get("truncated")),
    }


async def get_node_detail_for_engine(
    engine: str,
    *,
    tenant_id: str,
    node_id: str,
    allowed_doc_ids: set[str] | None = None,
) -> dict | None:
    if engine == "legacy":
        return get_node_detail(node_id)
    graph = await export_kg_for_engine(
        engine,
        tenant_id=tenant_id,
        allowed_doc_ids=allowed_doc_ids,
        complete=True,
    )
    node = next((item for item in graph.get("nodes", []) if item.get("id") == node_id), None)
    if not node:
        return None
    node_ids = {edge.get("source") for edge in graph.get("edges", []) if edge.get("target") == node_id}
    node_ids.update(edge.get("target") for edge in graph.get("edges", []) if edge.get("source") == node_id)
    return {**node, "neighbor_count": len(node_ids), "degree_centrality": 0.0}


async def get_neighbors_for_engine(
    engine: str,
    *,
    tenant_id: str,
    node_id: str,
    hops: int = 1,
    allowed_doc_ids: set[str] | None = None,
) -> dict | None:
    if engine == "legacy":
        return get_neighbors(node_id, hops)
    graph = await export_kg_for_engine(
        engine,
        tenant_id=tenant_id,
        allowed_doc_ids=allowed_doc_ids,
        complete=True,
    )
    nodes = {item.get("id"): item for item in graph.get("nodes", [])}
    if node_id not in nodes:
        return None
    adjacency: dict[str, set[str]] = {key: set() for key in nodes}
    for edge in graph.get("edges", []):
        source, target = edge.get("source"), edge.get("target")
        if source in adjacency and target in adjacency:
            adjacency[source].add(target)
            adjacency[target].add(source)
    visited = {node_id}
    frontier = {node_id}
    by_hop: dict[str, list[dict]] = {}
    for distance in range(1, max(1, min(hops, 5)) + 1):
        next_frontier = {neighbor for item in frontier for neighbor in adjacency.get(item, set()) if neighbor not in visited}
        visited.update(next_frontier)
        by_hop[str(distance)] = [nodes[item] for item in sorted(next_frontier)]
        frontier = next_frontier
    return {
        "center": nodes[node_id],
        "hops": hops,
        "neighbors_by_hop": by_hop,
        "total_neighbors": sum(len(items) for items in by_hop.values()),
    }


async def get_stats_for_engine(
    engine: str,
    *,
    tenant_id: str,
    allowed_doc_ids: set[str] | None = None,
) -> dict:
    if engine == "legacy":
        return get_stats(allowed_doc_ids)
    graph = await export_kg_for_engine(
        engine,
        tenant_id=tenant_id,
        allowed_doc_ids=allowed_doc_ids,
        complete=True,
    )
    type_distribution: dict[str, int] = {}
    relation_types: dict[str, int] = {}
    for node in graph.get("nodes", []):
        key = str(node.get("type") or "UNKNOWN")
        type_distribution[key] = type_distribution.get(key, 0) + 1
    for edge in graph.get("edges", []):
        key = str(edge.get("relation") or "RELATED_TO")
        relation_types[key] = relation_types.get(key, 0) + 1
    return {
        "engine": "lightrag",
        "total_nodes": graph.get("total_nodes", len(graph.get("nodes", []))),
        "total_edges": graph.get("total_edges", len(graph.get("edges", []))),
        "type_distribution": type_distribution,
        "relation_types": relation_types,
        "source_documents": sorted({str(node.get("source_doc")) for node in graph.get("nodes", []) if node.get("source_doc")}),
    }
