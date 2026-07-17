"""
KG Builder — node deduplication + CO_OCCURS_IN edge generation.
Independent implementation for the GraphRAG Studio backend.
"""
from __future__ import annotations

from collections import defaultdict

import langextract as lx

from pipeline.text_assembler import PageText
from pipeline.cooccurrence import build_sparse_cooccurrence_edges

ACCEPTED_ALIGNMENTS = {"match_exact", "match_greater", "match_lesser"}


def build_kg(
    pages: list[PageText],
    annotated_docs: list[lx.data.AnnotatedDocument],
    source_doc_id: str,
) -> tuple[list[dict], list[dict]]:
    """Build KG nodes and edges from LangExtract results.

    Returns:
        (nodes, edges) — deduplicated node list and edge list.
    """
    # Phase 1: collect raw entities
    raw_entities = []
    for page, doc in zip(pages, annotated_docs):
        if not doc.extractions:
            continue
        for ext in doc.extractions:
            status = ext.alignment_status.value if ext.alignment_status else None
            if status not in ACCEPTED_ALIGNMENTS:
                continue
            char_start = ext.char_interval.start_pos if ext.char_interval else None
            char_end = ext.char_interval.end_pos if ext.char_interval else None
            raw_entities.append({
                "name": ext.extraction_text,
                "type": ext.extraction_class,
                "char_start": char_start,
                "char_end": char_end,
                "confidence": status,
                "page": page.page_idx,
                "source_doc": source_doc_id,
            })

    # Phase 2: deduplicate nodes
    seen: dict[tuple[str, str], int] = {}
    nodes: list[dict] = []
    page_node_positions: dict[int, dict[int, int]] = defaultdict(dict)

    for entity_order, entity in enumerate(raw_entities):
        type_prefix = entity["type"].lower()[:4]
        name_slug = entity["name"].lower().replace(" ", "")[:12]
        dedup_key = (entity["name"].lower(), entity["type"])
        if dedup_key not in seen:
            node_idx = len(nodes)
            seen[dedup_key] = node_idx
            nodes.append({
                "id": f"{type_prefix}_{name_slug}_{node_idx}",
                "name": entity["name"],
                "type": entity["type"],
                "source_doc": entity["source_doc"],
                "char_start": entity["char_start"],
                "char_end": entity["char_end"],
                "confidence": entity["confidence"],
                "page": entity["page"],
            })
        node_idx = seen[dedup_key]
        page_idx = entity["page"]
        position = entity["char_start"] if entity["char_start"] is not None else entity_order
        current = page_node_positions[page_idx].get(node_idx)
        page_node_positions[page_idx][node_idx] = position if current is None else min(current, position)

    # Phase 3: nearby co-occurrence edges. Small physical pages keep their
    # complete graph; large logical Markdown pages use bounded local links.
    positioned_nodes = {
        page_idx: [(position, nodes[node_idx]["id"]) for node_idx, position in positions.items()]
        for page_idx, positions in page_node_positions.items()
    }
    edges = build_sparse_cooccurrence_edges(positioned_nodes, source_doc_id)

    return nodes, edges


def extractions_to_records(
    pages: list[PageText],
    annotated_docs: list[lx.data.AnnotatedDocument],
    doc_id: str,
) -> list[dict]:
    """Flatten LangExtract results to ExtractionRecord dicts."""
    records = []
    for page, doc in zip(pages, annotated_docs):
        if not doc.extractions:
            continue
        for ext in doc.extractions:
            status = ext.alignment_status.value if ext.alignment_status else None
            records.append({
                "text": ext.extraction_text,
                "type": ext.extraction_class,
                "char_start": ext.char_interval.start_pos if ext.char_interval else None,
                "char_end": ext.char_interval.end_pos if ext.char_interval else None,
                "alignment": status,
                "page": page.page_idx,
                "doc_id": doc_id,
            })
    return records
