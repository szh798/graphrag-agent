"""KG Service — graph operations through the active graph repository."""
from __future__ import annotations

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
