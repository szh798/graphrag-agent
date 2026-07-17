"""Sparse co-occurrence edge helpers for indexing and graph visualization."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


MAX_PAGE_CLIQUE_NODES = 12
MAX_NEIGHBORS_PER_NODE = 4


def _page(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _position(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def neighbor_pairs(
    page_nodes: Mapping[int, Iterable[tuple[int, str]]],
    *,
    clique_limit: int = MAX_PAGE_CLIQUE_NODES,
    max_neighbors: int = MAX_NEIGHBORS_PER_NODE,
) -> set[tuple[int, str, str]]:
    """Return deterministic nearby node pairs for each logical page.

    Small physical pages retain their complete co-occurrence graph. Large
    logical pages (notably Markdown files) connect each entity only to nearby
    entities in source order, avoiding an O(n²) complete graph.
    """
    selected: set[tuple[int, str, str]] = set()
    for page_idx, positioned_nodes in page_nodes.items():
        positions: dict[str, int] = {}
        for position, node_id in positioned_nodes:
            node_id = str(node_id or "")
            if not node_id:
                continue
            positions[node_id] = min(positions.get(node_id, position), position)

        ordered = sorted(positions, key=lambda node_id: (positions[node_id], node_id))
        reach = len(ordered) - 1 if len(ordered) <= clique_limit else max_neighbors
        for index, source in enumerate(ordered):
            for target in ordered[index + 1: index + 1 + reach]:
                left, right = sorted((source, target))
                selected.add((_page(page_idx), left, right))
    return selected


def build_sparse_cooccurrence_edges(
    page_nodes: Mapping[int, Iterable[tuple[int, str]]],
    doc_id: str,
) -> list[dict]:
    """Build durable co-occurrence edges without materializing page cliques."""
    return [
        {
            "source": source,
            "target": target,
            "relation": "CO_OCCURS_IN",
            "doc_id": doc_id,
            "page": page_idx,
        }
        for page_idx, source, target in sorted(neighbor_pairs(page_nodes))
    ]


def select_sparse_layout_edges(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Select an Obsidian-style sparse view from an existing dense graph.

    Real semantic relationships are always preserved. Only generated
    ``CO_OCCURS_IN`` cliques are reduced, using source positions when present.
    The underlying persisted graph is not mutated.
    """
    node_map = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    scoped_node_map = {
        (str(node.get("source_doc") or ""), str(node.get("id") or "")): node
        for node in nodes
        if node.get("id")
    }
    page_node_ids: dict[tuple[str, int], set[str]] = defaultdict(set)
    for edge in edges:
        if str(edge.get("relation") or "CO_OCCURS_IN").upper() != "CO_OCCURS_IN":
            continue
        doc_id = str(edge.get("doc_id") or "")
        page_idx = _page(edge.get("page"))
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if (doc_id, source) in scoped_node_map or source in node_map:
            page_node_ids[(doc_id, page_idx)].add(source)
        if (doc_id, target) in scoped_node_map or target in node_map:
            page_node_ids[(doc_id, page_idx)].add(target)

    allowed_pairs: set[tuple[str, int, str, str]] = set()
    for (doc_id, page_idx), node_ids in page_node_ids.items():
        positioned_nodes: list[tuple[int, str]] = []
        for fallback, node_id in enumerate(sorted(node_ids)):
            node = scoped_node_map.get((doc_id, node_id)) or node_map[node_id]
            positioned_nodes.append((_position(node.get("char_start"), fallback), node_id))
        allowed_pairs.update(
            (doc_id, pair_page, source, target)
            for pair_page, source, target in neighbor_pairs({page_idx: positioned_nodes})
        )

    selected: list[dict] = []
    seen: set[tuple[str, str, str, str, int]] = set()
    for edge in edges:
        relation = str(edge.get("relation") or "CO_OCCURS_IN").upper()
        doc_id = str(edge.get("doc_id") or "")
        page_idx = _page(edge.get("page"))
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        left, right = sorted((source, target))
        key = (doc_id, left, right, relation, page_idx)
        if key in seen:
            continue
        if relation == "CO_OCCURS_IN" and (doc_id, page_idx, left, right) not in allowed_pairs:
            continue
        seen.add(key)
        selected.append(edge)
    return selected
