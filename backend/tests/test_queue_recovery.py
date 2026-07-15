from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class DurableQueueRecoveryTests(unittest.TestCase):
    def test_upstash_claim_uses_a_lease_and_acknowledges_the_exact_receipt(self):
        from storage import queue_repository as queue

        raw = json.dumps({"job_id": "job_1", "doc_id": "doc_1", "attempt": 0}, separators=(",", ":"))
        repo = queue.UpstashRedisQueueRepository()
        commands: list[tuple[str, ...]] = []

        def command(*parts: str):
            commands.append(parts)
            return {"result": raw if parts[0] == "EVAL" else 1}

        with patch.object(repo, "_command", side_effect=command), patch.object(queue.time, "time", return_value=100):
            payload = repo.pop_index_job()
            repo.ack_index_job(payload or {})

        self.assertEqual(payload["job_id"], "job_1")
        self.assertEqual(payload["_queue_receipt"], raw)
        self.assertEqual(commands[0][0], "EVAL")
        self.assertEqual(commands[0][-1], str(100 + queue.INDEX_JOB_LEASE_SECONDS))
        self.assertEqual(commands[1], ("ZREM", queue.INDEX_PROCESSING_KEY, raw))

    def test_upstash_recovery_reports_requeued_and_exhausted_jobs(self):
        from storage import queue_repository as queue

        repo = queue.UpstashRedisQueueRepository()
        result = json.dumps({"recovered": ["job_retry"], "exhausted": ["job_failed"]})
        with patch.object(repo, "_command", return_value={"result": result}):
            recovery = repo.recover_expired_jobs()

        self.assertEqual(recovery, {"recovered": ["job_retry"], "exhausted": ["job_failed"]})

    def test_worker_updates_recovery_state_and_acks_completed_claim(self):
        from services import indexing_service as service

        jobs = {
            "job_retry": {"job_id": "job_retry", "doc_id": "doc_retry", "status": "parsing", "created_at": "2026-01-01T00:00:00+00:00"},
            "job_failed": {"job_id": "job_failed", "doc_id": "doc_failed", "status": "indexing", "created_at": "2026-01-01T00:00:00+00:00"},
        }
        saved: list[dict] = []
        acknowledged: list[dict] = []

        class AppRepo:
            def load_job_meta(self, job_id):
                return jobs.get(job_id)

            def save_job_meta(self, job_id, meta):
                jobs[job_id] = dict(meta)
                saved.append(dict(meta))

        class QueueRepo:
            def recover_expired_jobs(self):
                return {"recovered": ["job_retry"], "exhausted": ["job_failed"]}

            def pop_index_job(self, timeout_seconds):
                return {"job_id": "job_current", "_queue_receipt": "receipt"}

            def ack_index_job(self, payload):
                acknowledged.append(dict(payload))

        with (
            patch.object(service.queue_store, "get_queue_repository", return_value=QueueRepo()),
            patch.object(service.app_store, "get_app_repository", return_value=AppRepo()),
            patch.object(service, "run_queued_job", return_value={"job_id": "job_current", "status": "done"}),
            patch.object(service, "update_doc_status") as update_doc_status,
            patch.object(service, "report_event"),
        ):
            result = service.process_next_index_job(1)

        self.assertEqual(result["status"], "done")
        self.assertEqual(jobs["job_retry"]["status"], "queued")
        self.assertEqual(jobs["job_failed"]["status"], "failed")
        update_doc_status.assert_called_once_with("doc_failed", "failed")
        self.assertEqual(acknowledged[0]["_queue_receipt"], "receipt")
        self.assertTrue(saved)


if __name__ == "__main__":
    unittest.main()
