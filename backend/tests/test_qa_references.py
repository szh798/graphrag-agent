import unittest
from unittest.mock import patch

from services import qa_service


class LegacyReferenceFallbackTests(unittest.TestCase):
    def test_legacy_query_uses_entity_reference_when_no_chunk_was_retrieved(self):
        class StubAppRepository:
            def get_chat_session(self, session_id, owner_id):
                return None

            def load_documents_index(self):
                return {"doc-fresh": {"filename": "freshly-uploaded.docx"}}

            def append_query_history(self, record):
                pass

        class StubGraphRepository:
            def profile(self):
                return {"backend": "filesystem"}

            def export_kg(self):
                return {
                    "nodes": [{
                        "id": "entity-contract",
                        "name": "QL-2026-017",
                        "type": "CONCEPT",
                        "source_doc": "doc-fresh",
                        "page": 0,
                    }],
                    "edges": [],
                }

            def hybrid_retrieve(self, *args, **kwargs):
                return {"nodes": [], "edges": [], "chunks": []}

        with (
            patch.object(
                qa_service.app_store,
                "get_app_repository",
                return_value=StubAppRepository(),
            ),
            patch.object(
                qa_service.graph_store,
                "get_graph_repository",
                return_value=StubGraphRepository(),
            ),
            patch(
                "pipeline.qa_agent.run_qa",
                return_value={
                    "answer": "QL-2026-017",
                    "tool_calls": [],
                    "cited_nodes": ["entity-contract"],
                    "cited_chunks": [],
                    "usage": {},
                },
            ),
        ):
            result = qa_service.run_query(
                "合同编号是什么？",
                [],
                "visitor-1",
                persist_session=False,
            )

        self.assertEqual(result["answer"], "QL-2026-017")
        self.assertEqual(result["references"][0]["filename"], "freshly-uploaded.docx")
        self.assertEqual(result["references"][0]["page"], 1)

    def test_cited_entities_resolve_to_document_filename_and_human_page(self):
        class StubAppRepository:
            def load_documents_index(self):
                return {
                    "doc-fresh": {
                        "doc_id": "doc-fresh",
                        "filename": "freshly-uploaded.docx",
                    }
                }

        nodes = [
            {
                "id": "entity-contract",
                "name": "QL-2026-017",
                "type": "CONCEPT",
                "source_doc": "doc-fresh",
                "page": 0,
            },
            {
                "id": "entity-unrelated",
                "name": "Other",
                "source_doc": "doc-other",
                "page": 2,
            },
        ]

        with patch.object(
            qa_service.app_store,
            "get_app_repository",
            return_value=StubAppRepository(),
        ):
            references = qa_service._references_from_cited_nodes(
                ["entity-contract"],
                nodes,
            )

        self.assertEqual(references, [{
            "doc_id": "doc-fresh",
            "filename": "freshly-uploaded.docx",
            "page": 1,
            "chunk_id": "entity:entity-contract",
            "excerpt": "QL-2026-017",
        }])

    def test_cited_entities_deduplicate_same_document_page(self):
        class StubAppRepository:
            def load_documents_index(self):
                return {"doc-1": {"filename": "same-page.md"}}

        nodes = [
            {"id": "n1", "name": "One", "source_doc": "doc-1", "page": 0},
            {"id": "n2", "name": "Two", "source_doc": "doc-1", "page": 0},
        ]

        with patch.object(
            qa_service.app_store,
            "get_app_repository",
            return_value=StubAppRepository(),
        ):
            references = qa_service._references_from_cited_nodes(["n1", "n2"], nodes)

        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["filename"], "same-page.md")


if __name__ == "__main__":
    unittest.main()
