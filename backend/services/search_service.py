"""Search Service — entity, path, and graph search through graph repository."""
from __future__ import annotations

from collections import deque

from storage import graph_repository as graph_store


def _allowed_node_ids(allowed_doc_ids: set[str]) -> set[str]:
    repo = graph_store.get_graph_repository()
    ids: set[str] = set()
    for doc_id in allowed_doc_ids:
        ids.update(node.get("id") for node in repo.export_kg(doc_id).get("nodes", []) if node.get("id"))
    return ids


def search_entities(
    q: str,
    entity_type: str | None = None,
    limit: int = 15,
    allowed_doc_ids: set[str] | None = None,
) -> dict:
    result = graph_store.get_graph_repository().search_entities(q, entity_type, max(limit, 100))
    if allowed_doc_ids is not None:
        items = [item for item in result.get("items", []) if item.get("source_doc") in allowed_doc_ids]
        result["items"] = items[:limit]
        result["total"] = len(items)
    return result


def search_path(
    from_id: str,
    to_id: str,
    max_hops: int = 3,
    allowed_doc_ids: set[str] | None = None,
) -> dict | None:
    if allowed_doc_ids is None:
        return graph_store.get_graph_repository().search_path(from_id, to_id, max_hops)
    allowed_nodes = _allowed_node_ids(allowed_doc_ids)
    if from_id not in allowed_nodes or to_id not in allowed_nodes:
        return None
    result = graph_store.get_graph_repository().search_path(from_id, to_id, max_hops)
    if result is None:
        return None
    result["paths"] = [
        path for path in result.get("paths", [])
        if all(node.get("id") in allowed_nodes for node in path.get("nodes", []))
    ]
    result["total_paths"] = len(result["paths"])
    return result


def search_graph(
    q: str,
    include_neighbors: bool = False,
    allowed_doc_ids: set[str] | None = None,
) -> dict:
    result = graph_store.get_graph_repository().search_graph(q, include_neighbors)
    if allowed_doc_ids is None:
        return result
    allowed_nodes = _allowed_node_ids(allowed_doc_ids)
    result["matched_nodes"] = [
        node for node in result.get("matched_nodes", []) if node.get("id") in allowed_nodes
    ]
    result["subgraph_edges"] = [
        edge for edge in result.get("subgraph_edges", [])
        if edge.get("source") in allowed_nodes and edge.get("target") in allowed_nodes
    ]
    result["total_nodes"] = len(result["matched_nodes"])
    return result


async def search_entities_for_engine(
    engine: str,
    *,
    tenant_id: str,
    q: str,
    entity_type: str | None = None,
    limit: int = 15,
    allowed_doc_ids: set[str] | None = None,
) -> dict:
    if engine == "legacy":
        result = search_entities(q, entity_type, limit, allowed_doc_ids)
        result["engine"] = "legacy"
        return result
    if engine != "lightrag":
        raise ValueError("INVALID_ENGINE")
    from services import kg_service, lightrag_service

    result = await lightrag_service.search_entities(
        tenant_id=tenant_id,
        query=q,
        entity_type=entity_type,
        limit=limit,
        allowed_doc_ids=allowed_doc_ids,
        additional_tenants=kg_service._public_tenants(tenant_id, allowed_doc_ids),
    )
    items = []
    for raw in result.get("items") or result.get("nodes") or []:
        node = dict(raw)
        pages = list(node.get("pages") or ([node["page"]] if node.get("page") is not None else []))
        node.update({
            "engine": "lightrag",
            "pages": pages,
            "page": pages[0] if pages else 0,
            "source_doc": str(node.get("source_doc") or node.get("doc_id") or ""),
            "description": node.get("description") or "",
        })
        items.append(node)
    return {"query": q, "engine": "lightrag", "total": len(items), "items": items[:limit]}


async def search_path_for_engine(
    engine: str,
    *,
    tenant_id: str,
    from_id: str,
    to_id: str,
    max_hops: int = 3,
    allowed_doc_ids: set[str] | None = None,
) -> dict | None:
    if engine == "legacy":
        return search_path(from_id, to_id, max_hops, allowed_doc_ids)
    from services import kg_service

    graph = await kg_service.export_kg_for_engine(
        engine,
        tenant_id=tenant_id,
        allowed_doc_ids=allowed_doc_ids,
        complete=True,
    )
    nodes = {node.get("id"): node for node in graph.get("nodes", [])}
    if from_id not in nodes or to_id not in nodes:
        return None
    adjacency: dict[str, list[tuple[str, dict]]] = {node_id: [] for node_id in nodes}
    for edge in graph.get("edges", []):
        source, target = edge.get("source"), edge.get("target")
        if source in adjacency and target in adjacency:
            adjacency[source].append((target, edge))
            adjacency[target].append((source, edge))
    queue = deque([(from_id, [from_id], [])])
    paths = []
    while queue and len(paths) < 20:
        current, node_path, edge_path = queue.popleft()
        if len(edge_path) >= max_hops:
            continue
        for neighbor, edge in adjacency.get(current, []):
            if neighbor in node_path:
                continue
            next_nodes = [*node_path, neighbor]
            next_edges = [*edge_path, edge]
            if neighbor == to_id:
                paths.append({
                    "length": len(next_edges),
                    "nodes": [nodes[node_id] for node_id in next_nodes],
                    "edges": next_edges,
                })
            else:
                queue.append((neighbor, next_nodes, next_edges))
    return {
        "from": nodes[from_id],
        "to": nodes[to_id],
        "max_hops": max_hops,
        "paths": paths,
        "total_paths": len(paths),
        "engine": "lightrag",
    }


async def search_graph_for_engine(
    engine: str,
    *,
    tenant_id: str,
    q: str,
    include_neighbors: bool = False,
    allowed_doc_ids: set[str] | None = None,
) -> dict:
    if engine == "legacy":
        result = search_graph(q, include_neighbors, allowed_doc_ids)
        result["engine"] = "legacy"
        return result
    from services import kg_service

    matched = await search_entities_for_engine(
        engine,
        tenant_id=tenant_id,
        q=q,
        limit=100,
        allowed_doc_ids=allowed_doc_ids,
    )
    graph = await kg_service.export_kg_for_engine(
        engine,
        tenant_id=tenant_id,
        allowed_doc_ids=allowed_doc_ids,
        complete=True,
    )
    matched_ids = {item.get("id") for item in matched.get("items", [])}
    edges = [
        edge for edge in graph.get("edges", [])
        if edge.get("source") in matched_ids or edge.get("target") in matched_ids
    ]
    if include_neighbors:
        neighbor_ids = matched_ids | {edge.get("source") for edge in edges} | {edge.get("target") for edge in edges}
        nodes = [node for node in graph.get("nodes", []) if node.get("id") in neighbor_ids]
    else:
        nodes = list(matched.get("items", []))
    return {
        "query": q,
        "engine": "lightrag",
        "matched_nodes": nodes,
        "subgraph_edges": edges,
        "total_nodes": len(nodes),
    }
