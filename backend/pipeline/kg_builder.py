"""
KG Builder — node deduplication + CO_OCCURS_IN edge generation.
Independent implementation for the GraphRAG Studio backend.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import langextract as lx

from pipeline.text_assembler import PageText
from pipeline.cooccurrence import build_sparse_cooccurrence_edges

ACCEPTED_ALIGNMENTS = {"match_exact", "match_greater", "match_lesser"}
# Building a Python ``int`` offset for every source character is deliberately
# bounded.  Callers/deployments with a different memory budget may override
# this module-level setting, but oversized pages remain safely ungrounded by
# default instead of risking an out-of-memory failure.
MAX_NULL_ALIGNMENT_RECOVERY_PAGE_CHARS = 2_000_000


@dataclass(frozen=True)
class _ResolvedAlignment:
    status: str | None
    char_start: int | None
    char_end: int | None
    recovered: bool = False


def _normalize_for_grounding(text: str) -> tuple[str, list[int]]:
    """Normalize case and whitespace while retaining source offsets.

    This intentionally does not normalize punctuation or perform approximate
    string similarity.  A recovered extraction must still be composed of the
    exact source characters (apart from case and Unicode whitespace), which
    prevents a plausible-looking model hallucination from entering the graph.
    """
    normalized: list[str] = []
    source_offsets: list[int] = []
    for source_offset, character in enumerate(text):
        if character.isspace():
            continue
        for folded_character in character.casefold():
            normalized.append(folded_character)
            source_offsets.append(source_offset)
    return "".join(normalized), source_offsets


def _recover_null_alignment(page_text: str, extraction_text: str) -> tuple[int, int] | None:
    """Locate an unaligned extraction only when it has one unambiguous match."""
    if len(page_text) > MAX_NULL_ALIGNMENT_RECOVERY_PAGE_CHARS:
        return None
    normalized_page, source_offsets = _normalize_for_grounding(page_text)
    return _recover_null_alignment_from_normalized(
        normalized_page,
        source_offsets,
        extraction_text,
    )


def _recover_null_alignment_from_normalized(
    normalized_page: str,
    source_offsets: list[int],
    extraction_text: str,
) -> tuple[int, int] | None:
    """Locate an extraction using grounding data prepared once for its page."""
    normalized_extraction, _ = _normalize_for_grounding(extraction_text)
    if not normalized_extraction or not normalized_page:
        return None

    first = normalized_page.find(normalized_extraction)
    if first < 0:
        return None
    # Multiple occurrences cannot be mapped to a trustworthy source span from
    # an alignment-less extraction, so leave them rejected for LangExtract to
    # align on a future run.
    if normalized_page.find(normalized_extraction, first + 1) >= 0:
        return None

    last = first + len(normalized_extraction) - 1
    char_start = source_offsets[first]
    char_end = source_offsets[last] + 1
    return char_start, char_end


def _resolve_alignment(
    page_text: str,
    ext: lx.data.Extraction,
    *,
    normalized_page: tuple[str, list[int]] | None = None,
) -> _ResolvedAlignment:
    status = ext.alignment_status.value if ext.alignment_status else None
    char_start = ext.char_interval.start_pos if ext.char_interval else None
    char_end = ext.char_interval.end_pos if ext.char_interval else None
    if status is not None:
        return _ResolvedAlignment(status, char_start, char_end)

    if normalized_page is None:
        recovered_span = _recover_null_alignment(page_text, ext.extraction_text)
    else:
        recovered_span = _recover_null_alignment_from_normalized(
            normalized_page[0],
            normalized_page[1],
            ext.extraction_text,
        )
    if recovered_span is None:
        return _ResolvedAlignment(None, char_start, char_end)
    return _ResolvedAlignment(
        "match_fuzzy",
        recovered_span[0],
        recovered_span[1],
        recovered=True,
    )


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
        normalized_page: tuple[str, list[int]] | None = None
        for ext in doc.extractions:
            if (
                ext.alignment_status is None
                and normalized_page is None
                and len(page.text) <= MAX_NULL_ALIGNMENT_RECOVERY_PAGE_CHARS
            ):
                normalized_page = _normalize_for_grounding(page.text)
            resolved = _resolve_alignment(
                page.text,
                ext,
                normalized_page=normalized_page,
            )
            if resolved.status not in ACCEPTED_ALIGNMENTS and not resolved.recovered:
                continue
            raw_entities.append({
                "name": ext.extraction_text,
                "type": ext.extraction_class,
                "char_start": resolved.char_start,
                "char_end": resolved.char_end,
                "confidence": resolved.status,
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
        normalized_page: tuple[str, list[int]] | None = None
        for ext in doc.extractions:
            if (
                ext.alignment_status is None
                and normalized_page is None
                and len(page.text) <= MAX_NULL_ALIGNMENT_RECOVERY_PAGE_CHARS
            ):
                normalized_page = _normalize_for_grounding(page.text)
            resolved = _resolve_alignment(
                page.text,
                ext,
                normalized_page=normalized_page,
            )
            records.append({
                "text": ext.extraction_text,
                "type": ext.extraction_class,
                "char_start": resolved.char_start,
                "char_end": resolved.char_end,
                "alignment": resolved.status,
                "page": page.page_idx,
                "doc_id": doc_id,
            })
    return records
