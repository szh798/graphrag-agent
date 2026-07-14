from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

OWNER_ID = "default"


class ImmediateThread:
    def __init__(self, target, daemon=False):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


class QABatchTests(unittest.TestCase):
    def test_start_batch_persists_progress_after_each_question(self):
        from services import qa_service as svc

        saved = []

        def fake_save(_batch_id, meta):
            saved.append({
                "status": meta["status"],
                "completed": meta["completed"],
                "failed": meta["failed"],
                "results": list(meta["results"]),
            })

        def fake_run_query(question, history, owner_id, persist_session=True):
            self.assertEqual(owner_id, OWNER_ID)
            return {
                "id": f"q_{question}",
                "question": question,
                "answer": f"answer {question}",
                "tool_calls": [],
                "cited_nodes": [],
                "duration_seconds": 0.1,
                "timestamp": "2026-06-30T00:00:00+00:00",
            }

        with (
            patch.object(svc.fs, "save_batch_meta", side_effect=fake_save),
            patch.object(svc, "run_query", side_effect=fake_run_query),
            patch("threading.Thread", ImmediateThread),
        ):
            result = svc.start_batch(["q1", "q2"], OWNER_ID)

        self.assertEqual(result["status"], "submitted")
        self.assertEqual(saved[0]["status"], "submitted")
        self.assertEqual(saved[1]["status"], "running")
        self.assertEqual(saved[1]["completed"], 0)
        self.assertEqual(saved[2]["status"], "running")
        self.assertEqual(saved[2]["completed"], 1)
        self.assertEqual(saved[3]["status"], "running")
        self.assertEqual(saved[3]["completed"], 2)
        self.assertEqual(saved[-1]["status"], "done")
        self.assertEqual(saved[-1]["completed"], 2)
        self.assertEqual(saved[-1]["failed"], 0)

    def test_list_batches_returns_newest_first(self):
        from services import qa_service as svc

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(svc.fs, "_BASE", Path(tmp)):
                svc.fs.save_batch_meta("batch_old", {
                    "batch_id": "batch_old",
                    "total": 1,
                    "completed": 1,
                    "failed": 0,
                    "status": "done",
                    "created_at": "2026-06-29T00:00:00+00:00",
                    "updated_at": "2026-06-29T00:00:01+00:00",
                    "results": [{"question": "old", "answer": "done"}],
                })
                svc.fs.save_batch_meta("batch_new", {
                    "batch_id": "batch_new",
                    "total": 2,
                    "completed": 0,
                    "failed": 0,
                    "status": "running",
                    "created_at": "2026-06-30T00:00:00+00:00",
                    "updated_at": "2026-06-30T00:00:01+00:00",
                    "results": [],
                })

                result = svc.list_batches(OWNER_ID, page=1, page_size=10)

        self.assertEqual(result["total"], 2)
        self.assertEqual([item["batch_id"] for item in result["items"]], ["batch_new", "batch_old"])
        self.assertNotIn("results", result["items"][0])

    def test_cancel_batch_marks_running_batch_cancelled(self):
        from services import qa_service as svc

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(svc.fs, "_BASE", Path(tmp)):
                svc.fs.save_batch_meta("batch_1", {
                    "batch_id": "batch_1",
                    "total": 3,
                    "completed": 1,
                    "failed": 0,
                    "status": "running",
                    "created_at": "2026-06-30T00:00:00+00:00",
                    "updated_at": "2026-06-30T00:00:01+00:00",
                    "results": [{"question": "q1", "answer": "a1"}],
                })

                result = svc.cancel_batch("batch_1", OWNER_ID)
                saved = svc.get_batch_result("batch_1", OWNER_ID)

        self.assertEqual(result["batch_id"], "batch_1")
        self.assertEqual(result["previous_status"], "running")
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["cancel_requested"])
        self.assertEqual(saved["status"], "cancelled")
        self.assertTrue(saved["cancel_requested"])

    def test_batch_poll_advances_without_background_thread_on_vercel(self):
        from services import qa_service as svc

        def fake_run_query(question, history, owner_id, persist_session=True):
            self.assertEqual(owner_id, OWNER_ID)
            return {
                "id": f"q_{question}",
                "question": question,
                "answer": f"answer {question}",
                "tool_calls": [],
                "cited_nodes": [],
                "duration_seconds": 0.1,
                "timestamp": "2026-06-30T00:00:00+00:00",
            }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(svc.fs, "_BASE", Path(tmp)),
                patch.object(svc, "run_query", side_effect=fake_run_query),
                patch.dict("os.environ", {"VERCEL": "1"}),
                patch("threading.Thread", side_effect=AssertionError("serverless mode must not start a thread")),
            ):
                started = svc.start_batch(["q1", "q2"], OWNER_ID)
                first = svc.get_batch_result(started["batch_id"], OWNER_ID)
                second = svc.get_batch_result(started["batch_id"], OWNER_ID)

        self.assertEqual(first["status"], "running")
        self.assertEqual(first["completed"], 1)
        self.assertEqual(len(first["results"]), 1)
        self.assertNotIn("questions", first)
        self.assertEqual(second["status"], "done")
        self.assertEqual(second["completed"], 2)
        self.assertEqual(len(second["results"]), 2)
        self.assertNotIn("next_index", second)


if __name__ == "__main__":
    unittest.main()
