from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from lightrag_integration.normalization import (  # noqa: E402
    normalize_entity_search,
    normalize_graph,
    normalize_query_result,
    normalize_references,
)
from lightrag_integration.types import coerce_pages, source_path  # noqa: E402


class Page:
    def __init__(self, page_idx: int, text: str):
        self.page_idx = page_idx
        self.text = text


class LightRAGNormalizationTests(unittest.TestCase):
    def test_existing_page_text_becomes_one_based_and_ignores_empty_pages(self):
        pages = coerce_pages([Page(0, "first"), Page(1, "  "), Page(2, "third")])
        self.assertEqual([(page.page, page.content) for page in pages], [(1, "first"), (3, "third")])

    def test_query_references_recover_document_page_and_excerpt(self):
        raw = {
            "llm_response": {"content": "answer"},
            "data": {
                "references": [{
                    "reference_id": "7",
                    "file_path": source_path("doc-1", "report.pdf", 3),
                    "content": ["evidence one", "evidence two"],
                }],
                "entities": [{"entity_name": "LightRAG"}],
            },
            "metadata": {"usage": {"total_tokens": 12}, "model": "glm"},
        }
        result = normalize_query_result(raw, workspace="ws_" + "a" * 40, mode="mix")
        self.assertEqual(result["answer"], "answer")
        self.assertEqual(result["references"][0]["doc_id"], "doc-1")
        self.assertEqual(result["references"][0]["filename"], "report.pdf")
        self.assertEqual(result["references"][0]["page"], 3)
        self.assertIn("evidence two", result["references"][0]["excerpt"])
        self.assertEqual(result["cited_entities"], ["LightRAG"])

    def test_graph_ids_are_engine_prefixed_workspace_scoped_and_document_filtered(self):
        raw = {
            "nodes": [
                {
                    "id": "Alice",
                    "properties": {
                        "entity_type": "PERSON",
                        "description": "Researcher",
                        "file_path": source_path("allowed", "a.md", 1),
                    },
                },
                {
                    "id": "Secret",
                    "properties": {"file_path": source_path("denied", "b.md", 2)},
                },
            ],
            "edges": [{
                "source": "Alice",
                "target": "Secret",
                "properties": {"description": "knows", "file_path": source_path("denied", "b.md", 2)},
            }],
        }
        workspace = "ws_" + "b" * 40
        result = normalize_graph(raw, workspace=workspace, allowed_doc_ids={"allowed"})
        self.assertEqual(result["total_nodes"], 1)
        self.assertEqual(result["total_edges"], 0)
        self.assertTrue(result["nodes"][0]["id"].startswith("lightrag:node:"))
        self.assertEqual(result["nodes"][0]["pages"], [1])
        self.assertEqual(result["nodes"][0]["source_doc"], "allowed")

    def test_scoped_normalization_drops_records_with_missing_source_metadata(self):
        workspace = "ws_" + "c" * 40
        references = normalize_references(
            [{"reference_id": "missing", "content": "must not leak"}],
            allowed_doc_ids={"allowed"},
        )
        self.assertEqual(references, [])

        raw = {
            "nodes": [
                {"id": "Unknown", "properties": {"description": "hidden"}},
                {
                    "id": "Allowed",
                    "properties": {
                        "file_path": source_path("allowed", "ok.md", 1)
                    },
                },
            ],
            "edges": [
                {
                    "source": "Unknown",
                    "target": "Allowed",
                    "properties": {"description": "missing ownership"},
                }
            ],
        }
        graph = normalize_graph(
            raw,
            workspace=workspace,
            allowed_doc_ids={"allowed"},
        )
        self.assertEqual([node["name"] for node in graph["nodes"]], ["Allowed"])
        self.assertEqual(graph["edges"], [])

        search = normalize_entity_search(
            ["Unknown"],
            workspace=workspace,
            query="Unknown",
            entity_type=None,
            limit=10,
            allowed_doc_ids={"allowed"},
        )
        self.assertEqual(search["items"], [])

        query = normalize_query_result(
            {
                "answer": "answer",
                "data": {
                    "references": [{"reference_id": "missing"}],
                    "entities": [{"entity_name": "Unknown"}],
                },
            },
            workspace=workspace,
            mode="mix",
            allowed_doc_ids={"allowed"},
        )
        self.assertEqual(query["references"], [])
        self.assertEqual(query["cited_entities"], [])


if __name__ == "__main__":
    unittest.main()
