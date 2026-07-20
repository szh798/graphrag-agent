from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, *, query=None, payload=None):
        self.calls.append({"method": method, "path": path, "payload": payload})
        return {
            "code": 0,
            "data": {
                "answer": f"answer from {payload['engine']}",
                "references": [{"doc_id": "doc-1", "page": 2, "excerpt": "evidence"}],
                "cited_entities": ["Python"],
                "token_usage": {"total_tokens": 12},
            },
        }


class DualEngineEvaluationScriptTests(unittest.TestCase):
    def test_committed_acceptance_fixture_has_ten_documents_and_fifty_questions(self):
        from scripts import evaluate_dual_engine as evaluation

        root = BACKEND_DIR.parent / "evaluations" / "dual-engine"
        dataset = evaluation.load_dataset(root / "questions.jsonl")
        manifest = json.loads((root / "documents.manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(len(dataset), 50)
        self.assertEqual(len(manifest["documents"]), 10)
        self.assertEqual(len({doc for row in dataset for doc in row["document_ids"]}), 10)
        self.assertTrue({"markdown", "pdf", "office", "image", "html", "txt"}.issubset(
            {item["format"] for item in manifest["documents"]}
        ))
        for item in manifest["documents"]:
            fixture_path = root / item["path"]
            self.assertTrue(fixture_path.is_file())
            self.assertGreater(fixture_path.stat().st_size, 0)
            self.assertTrue((root / item["source_path"]).is_file())
            if item["format"] == "pdf":
                self.assertTrue(fixture_path.read_bytes().startswith(b"%PDF-"))
            elif item["format"] == "office":
                with zipfile.ZipFile(fixture_path) as archive:
                    self.assertIn("[Content_Types].xml", archive.namelist())
                    self.assertIn("word/document.xml", archive.namelist())
            elif item["format"] == "image":
                self.assertTrue(fixture_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_dataset_validation_and_case_matrix(self):
        from scripts import evaluate_dual_engine as evaluation

        with tempfile.TemporaryDirectory() as directory:
            dataset_path = Path(directory) / "questions.jsonl"
            dataset_path.write_text(
                json.dumps({
                    "id": "q1",
                    "question": "What?",
                    "document_ids": ["doc-1"],
                    "expected_reference_pages": [2],
                }) + "\n",
                encoding="utf-8",
            )
            dataset = evaluation.load_dataset(dataset_path, min_questions=1, min_documents=1)

        cases = evaluation.build_cases(dataset, modes=("local", "mix"))
        self.assertEqual(len(cases), 3)
        self.assertEqual(
            [(case["engine"], case["retrieval_mode"]) for case in cases],
            [("legacy", None), ("lightrag", "local"), ("lightrag", "mix")],
        )

    def test_query_payloads_and_reference_page_scoring(self):
        from scripts import evaluate_dual_engine as evaluation

        dataset = [{
            "id": "q1",
            "question": "What?",
            "document_ids": ["doc-1"],
            "expected_reference_pages": [2],
            "tags": [],
        }]
        cases = evaluation.build_cases(dataset, modes=("mix",))
        client = FakeClient()

        results = evaluation.run_cases(client, cases)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["reference_page_match"] for result in results))
        legacy_payload = client.calls[0]["payload"]
        lightrag_payload = client.calls[1]["payload"]
        self.assertNotIn("retrieval_mode", legacy_payload)
        self.assertEqual(lightrag_payload["retrieval_mode"], "mix")

    def test_result_artifact_is_owner_read_write_only(self):
        from scripts import evaluate_dual_engine as evaluation

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.jsonl"
            evaluation.write_results(output, [{"id": "q1", "answer": "sensitive"}])
            mode = stat.S_IMODE(output.stat().st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["id"], "q1")


if __name__ == "__main__":
    unittest.main()
