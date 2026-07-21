from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services import qa_service
from storage.graph_repository import (
    _LEXICAL_CHUNK_TEXT_LIMIT,
    _lexical_chunk_score,
    PostgresGraphRepository,
)


class FakeCursor:
    def __init__(self, rows: list[dict], calls: list[tuple[str, tuple]]):
        self.rows = rows
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query: str, params: tuple):
        self.calls.append((query, params))

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(self, rows: list[dict], calls: list[tuple[str, tuple]]):
        self.rows = rows
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return FakeCursor(self.rows, self.calls)


class PostgresChunkRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple[str, tuple]] = []
        self.rows = [
            {
                "chunk_id": "cn-1",
                "doc_id": "doc-cn",
                "payload": {
                    "chunk_id": "cn-1",
                    "doc_id": "doc-cn",
                    "page": 0,
                },
                "text": "枫叶诊所每逢周日均正常营业，门诊时间为九点至十七点。",
            },
            {
                "chunk_id": "en-1",
                "doc_id": "doc-en",
                "payload": {
                    "chunk_id": "en-1",
                    "doc_id": "doc-en",
                    "page": 1,
                },
                "text": "Project Qinglan uses contract number QL-2026-017.",
            },
            {
                "chunk_id": "secret-1",
                "doc_id": "doc-secret",
                "payload": {
                    "chunk_id": "secret-1",
                    "doc_id": "doc-secret",
                    "page": 0,
                },
                "text": "枫叶诊所周日是否营业：这是另一个租户的机密答案。",
            },
            {
                "chunk_id": "noise-1",
                "doc_id": "doc-cn",
                "payload": {
                    "chunk_id": "noise-1",
                    "doc_id": "doc-cn",
                    "page": 2,
                },
                "text": "天气晴朗，适合户外活动。",
            },
        ]
        self.repo = PostgresGraphRepository()
        self.repo._connect = lambda: FakeConnection(self.rows, self.calls)
        self.repo.search_graph = lambda *args, **kwargs: {
            "matched_nodes": [
                {"id": "allowed", "source_doc": "doc-cn"},
                {"id": "secret", "source_doc": "doc-secret"},
            ],
            "subgraph_edges": [
                {"id": "edge-allowed", "doc_id": "doc-cn"},
                {"id": "edge-secret", "doc_id": "doc-secret"},
            ],
        }

    def test_chinese_bigrams_match_with_intervening_words_and_apply_scope_in_sql(self):
        result = self.repo.hybrid_retrieve(
            "枫叶诊所周日是否营业",
            allowed_document_ids={"doc-cn"},
        )

        self.assertEqual([item["chunk_id"] for item in result["chunks"]], ["cn-1"])
        self.assertEqual([item["id"] for item in result["nodes"]], ["allowed"])
        self.assertEqual([item["id"] for item in result["edges"]], ["edge-allowed"])
        query, params = self.calls[-1]
        self.assertIn("WHERE doc_id = ANY(%s)", query)
        self.assertLess(
            query.index("WHERE doc_id = ANY(%s)"),
            query.index("CROSS JOIN LATERAL"),
        )
        self.assertEqual(params[0], ["doc-cn"])
        self.assertLessEqual(params[-1], 256)

    def test_english_project_name_matches_case_insensitively(self):
        chunks = self.repo.hybrid_retrieve(
            "What is the contract number for PROJECT QINGLAN?",
            allowed_document_ids={"doc-en"},
        )["chunks"]

        self.assertEqual([item["chunk_id"] for item in chunks], ["en-1"])

    def test_common_chinese_question_forms_retrieve_the_factual_chunk(self):
        factual_rows = [
            {
                "chunk_id": "contract-cn",
                "doc_id": "doc-facts",
                "payload": {"page": 0},
                "text": "合同编号：QL-2026-017。",
            },
            {
                "chunk_id": "address-cn",
                "doc_id": "doc-facts",
                "payload": {"page": 1},
                "text": "地址：杭州市文一路 88 号。",
            },
        ]
        self.repo._connect = lambda: FakeConnection(factual_rows, self.calls)

        cases = {
            "合同是什么": "contract-cn",
            "编号是多少": "contract-cn",
            "地址在哪里": "address-cn",
        }
        for question, expected_chunk_id in cases.items():
            with self.subTest(question=question):
                chunks = self.repo.hybrid_retrieve(
                    question,
                    allowed_document_ids={"doc-facts"},
                )["chunks"]
                self.assertEqual(
                    [item["chunk_id"] for item in chunks],
                    [expected_chunk_id],
                )

    def test_english_stopwords_and_substrings_do_not_create_false_matches(self):
        query = "What is the contract number for PROJECT QINGLAN?"
        self.assertGreater(
            _lexical_chunk_score(
                query,
                "Project Qinglan uses contract number QL-2026-017.",
            ),
            0,
        )
        self.assertEqual(
            _lexical_chunk_score(
                query,
                "This is another project for the weather report.",
            ),
            0,
        )
        self.assertEqual(
            _lexical_chunk_score("contract", "A subcontractor was selected."),
            0,
        )

    def test_sql_projects_a_bounded_match_window_and_python_caps_test_doubles(self):
        oversized = "needle marker " + ("x" * (_LEXICAL_CHUNK_TEXT_LIMIT * 3))
        rows = [{
            "chunk_id": "large-1",
            "doc_id": "doc-large",
            "payload": {"page": 0, "unrelated_metadata": "kept"},
            "text": oversized,
        }]
        self.repo._connect = lambda: FakeConnection(rows, self.calls)

        chunks = self.repo.hybrid_retrieve(
            "needle marker",
            allowed_document_ids={"doc-large"},
        )["chunks"]

        self.assertEqual(len(chunks), 1)
        self.assertLessEqual(len(chunks[0]["text"]), _LEXICAL_CHUNK_TEXT_LIMIT)
        query, _ = self.calls[-1]
        self.assertIn("payload - 'text' - 'content' AS payload", query)
        self.assertIn("substring(", query)
        self.assertIn("AS text", query)

    def test_unrelated_question_returns_no_chunks(self):
        chunks = self.repo.hybrid_retrieve(
            "北极星退款政策是什么",
            allowed_document_ids={"doc-cn", "doc-en"},
        )["chunks"]

        self.assertEqual(chunks, [])

    def test_empty_allowed_scope_does_not_query_database(self):
        chunks = self.repo.hybrid_retrieve(
            "Project Qinglan",
            allowed_document_ids=set(),
        )["chunks"]

        self.assertEqual(chunks, [])
        self.assertEqual(self.calls, [])

    def test_qa_call_passes_scope_and_keeps_legacy_stub_compatibility(self):
        captured: list[set[str] | None] = []

        class ScopedRepo:
            def hybrid_retrieve(self, question, embedding=None, allowed_document_ids=None):
                captured.append(allowed_document_ids)
                return {"nodes": [], "edges": [], "chunks": []}

        class LegacyStub:
            def hybrid_retrieve(self, question, embedding=None):
                return {"nodes": [], "edges": [], "chunks": []}

        scope = {"doc-cn"}
        qa_service._hybrid_retrieve_scoped(
            ScopedRepo(),
            "question",
            embedding=None,
            allowed_document_ids=scope,
        )
        legacy_result = qa_service._hybrid_retrieve_scoped(
            LegacyStub(),
            "question",
            embedding=None,
            allowed_document_ids=scope,
        )

        self.assertEqual(captured, [scope])
        self.assertEqual(legacy_result["chunks"], [])


if __name__ == "__main__":
    unittest.main()
