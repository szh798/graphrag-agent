from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class QASessionTests(unittest.TestCase):
    def test_session_history_preserves_existing_unbounded_service_contract(self):
        from services import qa_service as svc

        session = {
            "messages": [
                {"role": "human" if index % 2 == 0 else "ai", "content": str(index)}
                for index in range(12)
            ]
        }

        history = svc._session_history(session)

        self.assertEqual(len(history), 12)
        self.assertEqual(history[0]["content"], "0")
        self.assertEqual(history[-1]["content"], "11")

    def test_run_query_persists_session_and_uses_it_for_next_turn(self):
        from services import qa_service as svc

        owner_id = "11111111-1111-4111-8111-111111111111"
        histories_seen: list[list[dict]] = []

        def fake_run_qa(question, history, nodes, edges):
            histories_seen.append(history)
            return {
                "answer": f"answer for {question}",
                "tool_calls": [],
                "cited_nodes": [],
            }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(svc.fs, "_BASE", Path(tmp)),
                patch.object(svc.fs, "load_kg_nodes", return_value=[{"id": "n1"}]),
                patch.object(svc.fs, "load_kg_edges", return_value=[]),
                patch("pipeline.qa_agent.run_qa", side_effect=fake_run_qa),
            ):
                first = svc.run_query("第一问", [], owner_id, session_id=None)
                second = svc.run_query("第二问", [], owner_id, session_id=first["session_id"])
                session = svc.get_session(first["session_id"], owner_id)
                sessions = svc.get_sessions(owner_id, page=1, page_size=10)

        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(histories_seen[0], [])
        self.assertEqual(
            histories_seen[1],
            [
                {"role": "human", "content": "第一问"},
                {"role": "ai", "content": "answer for 第一问"},
            ],
        )
        self.assertEqual(session["message_count"], 4)
        self.assertEqual([m["role"] for m in session["messages"]], ["human", "ai", "human", "ai"])
        self.assertEqual(sessions["items"][0]["id"], first["session_id"])
        self.assertEqual(sessions["items"][0]["message_count"], 4)


if __name__ == "__main__":
    unittest.main()
