"""KG Service — graph operations through the active graph repository."""
from __future__ import annotations

from storage import graph_repository as graph_store


def get_nodes(page: int = 1, page_size: int = 50,
              node_type: str | None = None,
              doc_id: str | None = None,
              confidence: str | None = None) -> dict:
    return graph_store.get_graph_repository().get_nodes(page, page_size, node_type, doc_id, confidence)


def get_edges(page: int = 1, page_size: int = 100,
              doc_id: str | None = None,
              relation: str | None = None) -> dict:
    return graph_store.get_graph_repository().get_edges(page, page_size, doc_id, relation)


def get_node_detail(node_id: str) -> dict | None:
    return graph_store.get_graph_repository().get_node_detail(node_id)


def get_neighbors(node_id: str, hops: int = 1) -> dict | None:
    return graph_store.get_graph_repository().get_neighbors(node_id, hops)


def get_stats() -> dict:
    return graph_store.get_graph_repository().get_stats()


def export_kg(doc_id: str | None = None) -> dict:
    return graph_store.get_graph_repository().export_kg(doc_id)
