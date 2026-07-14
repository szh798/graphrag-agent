"""Search Service — entity, path, and graph search through graph repository."""
from __future__ import annotations

from storage import graph_repository as graph_store


def search_entities(q: str, entity_type: str | None = None, limit: int = 15) -> dict:
    return graph_store.get_graph_repository().search_entities(q, entity_type, limit)


def search_path(from_id: str, to_id: str, max_hops: int = 3) -> dict | None:
    return graph_store.get_graph_repository().search_path(from_id, to_id, max_hops)


def search_graph(q: str, include_neighbors: bool = False) -> dict:
    return graph_store.get_graph_repository().search_graph(q, include_neighbors)
