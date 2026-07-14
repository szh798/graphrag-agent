"""Import the local JSON knowledge graph into the configured graph repository.

This is intentionally small and idempotent: the Neo4j repository uses MERGE for
documents, entities, chunks, and relationships, so the script can be rerun after
setting GRAPHRAG_GRAPH_BACKEND=neo4j.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from storage import file_store as fs
from storage import graph_repository as graph_store

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def _group_by(items: list[dict[str, Any]], field: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        value = str(item.get(field) or "")
        grouped.setdefault(value, []).append(item)
    return grouped


def _summary(docs: dict[str, dict[str, Any]], nodes: list[dict[str, Any]], edges: list[dict[str, Any]], dry_run: bool) -> dict:
    return {
        "dry_run": dry_run,
        "documents": len(docs),
        "nodes": len(nodes),
        "edges": len(edges),
        "imported_documents": 0,
    }


def import_file_store_graph(dry_run: bool = True) -> dict:
    docs = fs.load_docs_index()
    nodes = fs.load_kg_nodes()
    edges = fs.load_kg_edges()
    result = _summary(docs, nodes, edges, dry_run)

    if dry_run:
        return result

    nodes_by_doc = _group_by(nodes, "source_doc")
    edges_by_doc = _group_by(edges, "doc_id")
    repo = graph_store.get_graph_repository()
    for doc_id, doc in docs.items():
        normalized_doc = {**doc, "doc_id": doc.get("doc_id") or doc_id}
        repo.upsert_document_graph(
            document=normalized_doc,
            nodes=nodes_by_doc.get(doc_id, []),
            edges=edges_by_doc.get(doc_id, []),
            chunks=[],
        )
        result["imported_documents"] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Import local JSON graph data into Neo4j or the configured graph backend.")
    parser.add_argument("--apply", action="store_true", help="write data instead of only printing a dry-run summary")
    args = parser.parse_args()
    print(json.dumps(import_file_store_graph(dry_run=not args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
