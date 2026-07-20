from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
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
            "job_current": {"job_id": "job_current", "doc_id": "doc_current", "owner_id": "tenant_current", "status": "queued", "created_at": "2026-01-01T00:00:00+00:00"},
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

    def test_worker_leases_claim_for_recovery_when_job_runner_raises(self):
        from services import indexing_service as service

        acknowledged: list[dict] = []

        class QueueRepo:
            def recover_expired_jobs(self):
                return {"recovered": [], "exhausted": []}

            def pop_index_job(self, _timeout_seconds):
                return {"job_id": "job_broken", "_queue_receipt": "receipt"}

            def ack_index_job(self, payload):
                acknowledged.append(dict(payload))

        with (
            patch.object(service.queue_store, "get_queue_repository", return_value=QueueRepo()),
            patch.object(service.app_store, "get_app_repository"),
            patch.object(service, "run_queued_job", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                service.process_next_index_job(1)

        self.assertEqual(acknowledged, [])

    def test_upstash_owner_lock_and_processing_lease_use_compare_and_refresh_scripts(self):
        from storage import queue_repository as queue

        repo = queue.UpstashRedisQueueRepository()
        commands: list[tuple[str, ...]] = []

        def command(*parts: str):
            commands.append(parts)
            if parts[0] == "SET":
                return {"result": "OK"}
            return {"result": 1}

        payload = {"job_id": "job_1", "_queue_receipt": "receipt"}
        with (
            patch.object(repo, "_command", side_effect=command),
            patch.object(queue.time, "time", return_value=100),
        ):
            self.assertTrue(repo.acquire_index_owner_lock("tenant-secret", "job_1"))
            self.assertTrue(repo.refresh_index_owner_lock("tenant-secret", "job_1"))
            self.assertTrue(repo.refresh_index_job_lease(payload))
            self.assertTrue(repo.release_index_owner_lock("tenant-secret", "job_1"))

        lock_key = commands[0][1]
        self.assertNotIn("tenant-secret", lock_key)
        self.assertEqual(commands[0][0], "SET")
        self.assertIn("NX", commands[0])
        self.assertIn("EX", commands[0])
        self.assertEqual(commands[2][-2], "receipt")
        self.assertEqual(commands[2][-1], str(100 + queue.INDEX_JOB_LEASE_SECONDS))

    def test_upstash_worker_heartbeat_has_ttl_and_reports_freshness(self):
        from storage import queue_repository as queue

        repo = queue.UpstashRedisQueueRepository()
        commands: list[tuple[str, ...]] = []
        stored: dict[str, str] = {}

        def command(*parts: str):
            commands.append(parts)
            if parts[0] == "SET":
                stored[parts[1]] = parts[2]
                return {"result": "OK"}
            if parts[0] == "GET":
                return {"result": stored.get(parts[1])}
            raise AssertionError(parts)

        with (
            patch.dict(os.environ, {
                "INDEX_WORKER_HEARTBEAT_KEY": "test:worker-heartbeat",
                "INDEX_WORKER_HEARTBEAT_TTL_SECONDS": "90",
            }, clear=False),
            patch.object(repo, "_command", side_effect=command),
            patch.object(queue.time, "time", return_value=100),
        ):
            repo.record_worker_heartbeat("worker-stable", "1.2.3", last_seen=100)

        with (
            patch.dict(os.environ, {
                "INDEX_WORKER_HEARTBEAT_KEY": "test:worker-heartbeat",
                "INDEX_WORKER_HEARTBEAT_TTL_SECONDS": "90",
            }, clear=False),
            patch.object(repo, "_command", side_effect=command),
            patch.object(queue.time, "time", return_value=125),
        ):
            heartbeat = repo.get_worker_heartbeat()

        self.assertEqual(commands[0], (
            "SET",
            "test:worker-heartbeat",
            stored["test:worker-heartbeat"],
            "EX",
            "90",
        ))
        self.assertEqual(commands[1], ("GET", "test:worker-heartbeat"))
        self.assertEqual(heartbeat["worker_id"], "worker-stable")
        self.assertEqual(heartbeat["version"], "1.2.3")
        self.assertEqual(heartbeat["age_seconds"], 25)
        self.assertTrue(heartbeat["fresh"])

    def test_worker_heartbeat_identity_is_reused_and_not_derived_from_pid(self):
        from scripts import worker

        recorded: list[tuple[str, str]] = []

        class QueueRepo:
            def record_worker_heartbeat(self, worker_id, version):
                recorded.append((worker_id, version))
                return {"fresh": True}

        with (
            patch.dict(os.environ, {
                "INDEX_WORKER_ID": "",
                "RAILWAY_REPLICA_ID": "",
            }, clear=False),
            patch.object(worker, "_WORKER_ID", None),
            patch.object(worker.uuid, "uuid4", return_value=SimpleNamespace(hex="stable-random-id")),
            patch.object(os, "getpid", side_effect=AssertionError("PID must not be used")),
        ):
            worker._record_worker_heartbeat(QueueRepo())
            worker._record_worker_heartbeat(QueueRepo())

        self.assertEqual(recorded[0], recorded[1])
        self.assertEqual(recorded[0][0], "worker_stable-random-id")
        self.assertEqual(recorded[0][1], worker.APP_VERSION)


if __name__ == "__main__":
    unittest.main()
