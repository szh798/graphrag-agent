from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class ImportFileStoreToNeo4jTests(unittest.TestCase):
    def test_import_groups_nodes_and_edges_by_document(self):
        from scripts import import_file_store_to_neo4j as importer

        calls = []

        class StubRepo:
            def upsert_document_graph(self, document, nodes, edges, chunks=None):
                calls.append({"document": document, "nodes": nodes, "edges": edges, "chunks": chunks or []})

        with (
            patch.object(importer.fs, "load_docs_index", return_value={
                "doc_1": {"doc_id": "doc_1", "filename": "one.pdf", "status": "indexed"},
                "doc_2": {"doc_id": "doc_2", "filename": "two.pdf", "status": "indexed"},
            }),
            patch.object(importer.fs, "load_kg_nodes", return_value=[
                {"id": "n1", "name": "Python", "type": "TECHNOLOGY", "source_doc": "doc_1"},
                {"id": "n2", "name": "Neo4j", "type": "TECHNOLOGY", "source_doc": "doc_2"},
            ]),
            patch.object(importer.fs, "load_kg_edges", return_value=[
                {"source": "n1", "target": "n2", "relation": "RELATED_TO", "doc_id": "doc_1"},
            ]),
            patch.object(importer.graph_store, "get_graph_repository", return_value=StubRepo()),
        ):
            result = importer.import_file_store_graph(dry_run=False)

        self.assertEqual(result["documents"], 2)
        self.assertEqual(result["nodes"], 2)
        self.assertEqual(result["edges"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["document"]["doc_id"], "doc_1")
        self.assertEqual(calls[0]["nodes"][0]["id"], "n1")
        self.assertEqual(calls[0]["edges"][0]["source"], "n1")

    def test_import_dry_run_does_not_write(self):
        from scripts import import_file_store_to_neo4j as importer

        class StubRepo:
            def upsert_document_graph(self, document, nodes, edges, chunks=None):
                raise AssertionError("dry run must not write")

        with (
            patch.object(importer.fs, "load_docs_index", return_value={"doc_1": {"doc_id": "doc_1"}}),
            patch.object(importer.fs, "load_kg_nodes", return_value=[{"id": "n1", "source_doc": "doc_1"}]),
            patch.object(importer.fs, "load_kg_edges", return_value=[]),
            patch.object(importer.graph_store, "get_graph_repository", return_value=StubRepo()),
        ):
            result = importer.import_file_store_graph(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["documents"], 1)


if __name__ == "__main__":
    unittest.main()
