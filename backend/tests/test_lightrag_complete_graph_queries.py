from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services import kg_service, lightrag_service, search_service  # noqa: E402


NODE_COUNT = 250
EDGE_COUNT = 2500
LAYOUT_NODE_CAP = 200
LAYOUT_EDGE_CAP = 2000
DOC_ID = "doc-large"
TENANT_ID = "tenant-large"


def _node_id(index: int) -> str:
    return f"lightrag:node:{index:03d}"


def _large_graph() -> tuple[list[dict], list[dict]]:
    """Build a graph whose useful tail is beyond both layout caps."""
    nodes = [
        {
            "id": _node_id(index),
            "name": f"Entity {index:03d}",
            "type": "CONCEPT" if index < 210 else "PERSON",
            "description": f"Description {index:03d}",
            "source_doc": DOC_ID,
            "pages": [(index % 5) + 1],
            "char_start": index,
        }
        for index in range(NODE_COUNT)
    ]

    edges: list[dict] = []

    def add_edge(source: int, target: int) -> None:
        index = len(edges)
        edges.append(
            {
                "id": f"lightrag:edge:{index:04d}",
                "source": _node_id(source),
                "target": _node_id(target),
                "relation": "CO_OCCURS_IN",
                "description": "Co-occurs on the same page",
                "weight": 1.0,
                "doc_id": DOC_ID,
                "source_doc": DOC_ID,
                "pages": [1],
                "page": 1,
            }
        )

    # Include the local-neighbor pairs selected by the Obsidian-style sparse
    # layout, then fill the capped prefix with dense/repeated co-occurrences.
    for distance in range(1, 5):
        for source in range(LAYOUT_NODE_CAP - distance):
            add_edge(source, source + distance)
    filler = 0
    while len(edges) < LAYOUT_EDGE_CAP:
        source = filler % LAYOUT_NODE_CAP
        target = (filler * 37 + 17) % LAYOUT_NODE_CAP
        if source != target:
            add_edge(source, target)
        filler += 1

    # These relationships deliberately live beyond edge 2000 and involve
    # nodes beyond node 200. Capped display data cannot answer these queries.
    add_edge(200, 220)
    add_edge(220, 249)
    add_edge(248, 249)

    filler = 0
    while len(edges) < EDGE_COUNT:
        source = 200 + (filler % 50)
        target = 200 + ((filler * 11 + 7) % 50)
        if source != target:
            add_edge(source, target)
        filler += 1

    return nodes, edges


class LightRAGCompleteGraphQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.nodes, self.edges = _large_graph()
        self.export_calls: list[tuple[int, int]] = []
        self.env = patch.dict(
            os.environ,
            {
                "LIGHTRAG_MAX_GRAPH_NODES": str(LAYOUT_NODE_CAP),
                "LIGHTRAG_MAX_GRAPH_EDGES": str(LAYOUT_EDGE_CAP),
                "LIGHTRAG_EXPORT_MAX_NODES": "10000",
                "LIGHTRAG_EXPORT_MAX_EDGES": "100000",
            },
            clear=False,
        )
        self.env.start()

        async def export_graph(**kwargs):
            max_nodes = int(kwargs["max_nodes"])
            max_edges = int(kwargs["max_edges"])
            self.export_calls.append((max_nodes, max_edges))
            return {
                "total_nodes": len(self.nodes),
                "total_edges": len(self.edges),
                "nodes": self.nodes[:max_nodes],
                "edges": self.edges[:max_edges],
            }

        self.export_patch = patch.object(
            lightrag_service,
            "export_graph",
            side_effect=export_graph,
        )
        self.export_patch.start()

    def tearDown(self) -> None:
        self.export_patch.stop()
        self.env.stop()

    async def test_layout_export_remains_capped_and_edges_remain_sparse(self):
        graph = await kg_service.export_kg_for_engine(
            "lightrag",
            tenant_id=TENANT_ID,
        )
        nodes = await kg_service.get_nodes_for_engine(
            "lightrag",
            tenant_id=TENANT_ID,
            layout=True,
            page=1,
            page_size=1000,
        )
        layout = await kg_service.get_edges_for_engine(
            "lightrag",
            tenant_id=TENANT_ID,
            layout=True,
            page=1,
            page_size=5000,
        )

        self.assertEqual(len(graph["nodes"]), LAYOUT_NODE_CAP)
        self.assertEqual(len(graph["edges"]), LAYOUT_EDGE_CAP)
        self.assertEqual(nodes["total"], LAYOUT_NODE_CAP)
        self.assertEqual(len(nodes["items"]), LAYOUT_NODE_CAP)
        self.assertEqual(layout["raw_total"], LAYOUT_EDGE_CAP)
        self.assertGreater(len(layout["items"]), 0)
        self.assertLess(len(layout["items"]), layout["raw_total"])
        self.assertEqual(
            self.export_calls,
            [
                (LAYOUT_NODE_CAP, LAYOUT_EDGE_CAP),
                (LAYOUT_NODE_CAP, LAYOUT_EDGE_CAP),
                (LAYOUT_NODE_CAP, LAYOUT_EDGE_CAP),
            ],
        )

    async def test_node_and_edge_pagination_reach_items_beyond_layout_caps(self):
        node_page = await kg_service.get_nodes_for_engine(
            "lightrag",
            tenant_id=TENANT_ID,
            page=5,
            page_size=50,
        )
        edge_page = await kg_service.get_edges_for_engine(
            "lightrag",
            tenant_id=TENANT_ID,
            page=25,
            page_size=100,
        )

        self.assertEqual(node_page["total"], NODE_COUNT)
        self.assertEqual(len(node_page["items"]), 50)
        self.assertEqual(node_page["items"][-1]["id"], _node_id(249))
        self.assertEqual(edge_page["total"], EDGE_COUNT)
        self.assertEqual(edge_page["raw_total"], EDGE_COUNT)
        self.assertEqual(len(edge_page["items"]), 100)
        self.assertEqual(edge_page["items"][-1]["id"], "lightrag:edge:2499")
        self.assertEqual(self.export_calls, [(10000, 100000), (10000, 100000)])

    async def test_detail_neighbors_and_path_use_the_complete_graph(self):
        target_id = _node_id(249)
        detail = await kg_service.get_node_detail_for_engine(
            "lightrag",
            tenant_id=TENANT_ID,
            node_id=target_id,
        )
        neighbors = await kg_service.get_neighbors_for_engine(
            "lightrag",
            tenant_id=TENANT_ID,
            node_id=target_id,
            hops=1,
        )
        path_result = await search_service.search_path_for_engine(
            "lightrag",
            tenant_id=TENANT_ID,
            from_id=_node_id(200),
            to_id=target_id,
            max_hops=2,
        )

        self.assertIsNotNone(detail)
        self.assertEqual(detail["id"], target_id)
        self.assertGreaterEqual(detail["neighbor_count"], 2)
        self.assertIsNotNone(neighbors)
        neighbor_ids = {item["id"] for item in neighbors["neighbors_by_hop"]["1"]}
        self.assertTrue({_node_id(220), _node_id(248)} <= neighbor_ids)
        self.assertIsNotNone(path_result)
        self.assertGreater(path_result["total_paths"], 0)
        self.assertTrue(
            any(
                [node["id"] for node in item["nodes"]]
                == [_node_id(200), _node_id(220), target_id]
                for item in path_result["paths"]
            )
        )
        self.assertEqual(self.export_calls, [(10000, 100000)] * 3)

    async def test_graph_search_can_expand_a_match_beyond_the_layout_caps(self):
        target = self.nodes[249]

        async def search_entities(**kwargs):
            self.assertEqual(kwargs["query"], "Entity 249")
            return {"items": [target]}

        with patch.object(
            lightrag_service,
            "search_entities",
            side_effect=search_entities,
        ):
            result = await search_service.search_graph_for_engine(
                "lightrag",
                tenant_id=TENANT_ID,
                q="Entity 249",
                include_neighbors=True,
            )

        ids = {node["id"] for node in result["matched_nodes"]}
        self.assertIn(_node_id(249), ids)
        self.assertIn(_node_id(220), ids)
        self.assertIn(_node_id(248), ids)
        self.assertTrue(
            any(
                edge["source"] == _node_id(220)
                and edge["target"] == _node_id(249)
                for edge in result["subgraph_edges"]
            )
        )
        self.assertEqual(self.export_calls, [(10000, 100000)])

    async def test_stats_distributions_are_computed_from_the_complete_graph(self):
        result = await kg_service.get_stats_for_engine(
            "lightrag",
            tenant_id=TENANT_ID,
        )

        self.assertEqual(result["total_nodes"], NODE_COUNT)
        self.assertEqual(result["total_edges"], EDGE_COUNT)
        self.assertEqual(
            result["type_distribution"],
            {"CONCEPT": 210, "PERSON": 40},
        )
        self.assertEqual(result["relation_types"], {"CO_OCCURS_IN": EDGE_COUNT})
        self.assertEqual(result["source_documents"], [DOC_ID])
        self.assertEqual(self.export_calls, [(10000, 100000)])


if __name__ == "__main__":
    unittest.main()
