from __future__ import annotations

import unittest


class QAStreamTests(unittest.TestCase):
    def test_result_to_stream_events_emits_tool_calls_answer_chunks_and_done(self):
        from services import qa_service as svc

        result = {
            "id": "q_1",
            "question": "q",
            "answer": "hello world",
            "tool_calls": [
                {
                    "step": 1,
                    "tool_name": "describe_graph",
                    "tool_input": "{}",
                    "tool_output": "Nodes: 2",
                }
            ],
            "cited_nodes": [],
            "duration_seconds": 0.1,
            "timestamp": "2026-06-30T00:00:00+00:00",
        }

        events = list(svc.result_to_stream_events(result, chunk_size=5))

        self.assertEqual(events[0]["event"], "tool_call")
        self.assertEqual(events[0]["data"]["tool_name"], "describe_graph")
        answer = "".join(e["data"]["text"] for e in events if e["event"] == "answer_delta")
        self.assertEqual(answer, "hello world")
        self.assertEqual(events[-1], {"event": "done", "data": result})


if __name__ == "__main__":
    unittest.main()
