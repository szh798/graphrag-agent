from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _complete_graph(node_count: int, *, page: int = 0) -> tuple[list[dict], list[dict]]:
    nodes = [
        {"id": f"n{index:03d}", "char_start": index * 20, "page": page}
        for index in range(node_count)
    ]
    edges = [
        {
            "id": f"e-{left}-{right}",
            "source": nodes[left]["id"],
            "target": nodes[right]["id"],
            "relation": "CO_OCCURS_IN",
            "doc_id": "doc-1",
            "page": page,
        }
        for left in range(node_count)
        for right in range(left + 1, node_count)
    ]
    return nodes, edges


class CooccurrenceTests(unittest.TestCase):
    def test_large_markdown_complete_graph_is_reduced_to_local_links(self):
        from pipeline.cooccurrence import select_sparse_layout_edges

        nodes, edges = _complete_graph(193)
        selected = select_sparse_layout_edges(nodes, edges)

        self.assertEqual(len(edges), 18_528)
        self.assertEqual(len(selected), 762)
        for edge in selected:
            left = int(edge["source"][1:])
            right = int(edge["target"][1:])
            self.assertLessEqual(abs(left - right), 4)

    def test_small_physical_page_keeps_complete_graph(self):
        from pipeline.cooccurrence import select_sparse_layout_edges

        nodes, edges = _complete_graph(10)

        self.assertEqual(select_sparse_layout_edges(nodes, edges), edges)

    def test_semantic_edges_are_always_preserved(self):
        from pipeline.cooccurrence import select_sparse_layout_edges

        nodes, edges = _complete_graph(20)
        semantic = {
            "id": "semantic",
            "source": "n000",
            "target": "n019",
            "relation": "DEPENDS_ON",
            "doc_id": "doc-1",
            "page": 0,
        }

        selected = select_sparse_layout_edges(nodes, [*edges, semantic])

        self.assertIn(semantic, selected)

    def test_future_index_builds_sparse_edges_directly(self):
        from pipeline.cooccurrence import build_sparse_cooccurrence_edges

        positioned = {0: [(index * 20, f"n{index:03d}") for index in range(193)]}
        edges = build_sparse_cooccurrence_edges(positioned, "doc-1")

        self.assertEqual(len(edges), 762)
        self.assertTrue(all(edge["relation"] == "CO_OCCURS_IN" for edge in edges))

    def test_documents_are_sparsified_independently(self):
        from pipeline.cooccurrence import select_sparse_layout_edges

        first_nodes, first_edges = _complete_graph(20)
        second_nodes, second_edges = _complete_graph(20)
        for node in first_nodes:
            node["source_doc"] = "doc-1"
        for node in second_nodes:
            node["source_doc"] = "doc-2"
        for edge in second_edges:
            edge["doc_id"] = "doc-2"

        selected = select_sparse_layout_edges(
            [*first_nodes, *second_nodes], [*first_edges, *second_edges]
        )

        self.assertEqual(len(selected), 140)
        self.assertEqual({edge["doc_id"] for edge in selected}, {"doc-1", "doc-2"})


if __name__ == "__main__":
    unittest.main()
