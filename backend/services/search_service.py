"""Search Service — entity, path, and graph search through graph repository."""
from __future__ import annotations

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
