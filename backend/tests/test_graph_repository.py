from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class FakeNeo4jDriver:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.verified = False

    def execute_query(self, query: str, parameters_: dict | None = None, **kwargs):
        self.calls.append((query, parameters_ or kwargs))
        return [], None, []

    def verify_connectivity(self):
        self.verified = True

    def close(self):
        pass


class GraphRepositoryTests(unittest.TestCase):
    def test_neo4j_repository_creates_schema_and_upserts_graph_model(self):
        fake_driver = FakeNeo4jDriver()
        fake_neo4j = types.SimpleNamespace(
            GraphDatabase=types.SimpleNamespace(driver=lambda *args, **kwargs: fake_driver)
        )

        with (
            patch.dict(sys.modules, {"neo4j": fake_neo4j}),
            patch.dict("os.environ", {
                "NEO4J_URI": "neo4j+s://example.databases.neo4j.io",
                "NEO4J_USERNAME": "neo4j",
                "NEO4J_PASSWORD": "secret",
                "NEO4J_DATABASE": "neo4j",
                "NEO4J_VECTOR_DIMENSIONS": "3",
            }, clear=False),
        ):
            graph_repository = importlib.import_module("storage.graph_repository")
            repo = graph_repository.Neo4jGraphRepository()
            repo.ensure_schema()
            repo.upsert_document_graph(
                document={"doc_id": "doc_1", "owner_id": "u_1", "filename": "demo.pdf", "status": "indexed"},
                nodes=[{"id": "n1", "name": "Python", "type": "TECHNOLOGY", "confidence": "match_exact", "page": 1, "source_doc": "doc_1", "embedding": [0.1, 0.2, 0.3]}],
                edges=[{"source": "n1", "target": "n2", "relation": "RELATED_TO", "doc_id": "doc_1", "page": 1, "weight": 0.8}],
                chunks=[{"chunk_id": "c1", "doc_id": "doc_1", "page": 1, "text": "Python text", "embedding": [0.2, 0.2, 0.2], "entity_ids": ["n1"]}],
            )

        cypher = "\n".join(query for query, _ in fake_driver.calls)
        self.assertIn("CREATE CONSTRAINT graph_document_id", cypher)
        self.assertIn("CREATE FULLTEXT INDEX graph_entity_name_fulltext", cypher)
        self.assertIn("CREATE VECTOR INDEX graph_chunk_embedding_vector", cypher)
        self.assertIn("MERGE (d:Document", cypher)
        self.assertIn("MERGE (e:Entity", cypher)
        self.assertIn("MERGE (d)-[:HAS_CHUNK]->(c)", cypher)
        self.assertIn("MERGE (c)-[:MENTIONS]->(e)", cypher)
        self.assertIn("MERGE (s)-[r:RELATED_TO]->(t)", cypher)

    def test_repository_factory_uses_neo4j_only_when_configured(self):
        with patch.dict("os.environ", {"GRAPHRAG_GRAPH_BACKEND": "filesystem"}, clear=False):
            graph_repository = importlib.import_module("storage.graph_repository")
            repo = graph_repository.get_graph_repository()
            self.assertEqual(repo.profile()["backend"], "filesystem")

        fake_driver = FakeNeo4jDriver()
        fake_neo4j = types.SimpleNamespace(
            GraphDatabase=types.SimpleNamespace(driver=lambda *args, **kwargs: fake_driver)
        )
        with (
            patch.dict(sys.modules, {"neo4j": fake_neo4j}),
            patch.dict("os.environ", {
                "GRAPHRAG_GRAPH_BACKEND": "neo4j",
                "NEO4J_URI": "neo4j+s://example.databases.neo4j.io",
                "NEO4J_USERNAME": "neo4j",
                "NEO4J_PASSWORD": "secret",
            }, clear=False),
        ):
            repo = graph_repository.get_graph_repository()
            self.assertEqual(repo.profile()["backend"], "neo4j")

    def test_kg_and_search_services_delegate_to_graph_repository(self):
        kg_service = importlib.import_module("services.kg_service")
        search_service = importlib.import_module("services.search_service")

        class StubRepo:
            def get_nodes(self, *args, **kwargs):
                return {"total": 1, "page": 1, "page_size": 50, "items": [{"id": "n1"}]}

            def search_path(self, *args, **kwargs):
                return {"paths": [{"nodes": [{"id": "n1"}], "edges": [], "length": 0}], "total_paths": 1}

        with (
            patch.object(kg_service.graph_store, "get_graph_repository", return_value=StubRepo()),
            patch.object(search_service.graph_store, "get_graph_repository", return_value=StubRepo()),
        ):
            self.assertEqual(kg_service.get_nodes()["items"][0]["id"], "n1")
            self.assertEqual(search_service.search_path("n1", "n2")["total_paths"], 1)

    def test_health_reports_graph_database_component(self):
        system = importlib.import_module("routers.system")

        class StubRepo:
            def health(self):
                return {"status": "ok", "backend": "neo4j", "uri_configured": True}

        with patch.object(system.graph_store, "get_graph_repository", return_value=StubRepo()):
            response = asyncio.run(system.health_check())

        self.assertEqual(response.data["components"]["graph_database"]["status"], "ok")
        self.assertEqual(response.data["components"]["graph_database"]["backend"], "neo4j")


if __name__ == "__main__":
    unittest.main()
