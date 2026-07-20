"""Long-running indexing worker entrypoint."""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from services import indexing_service
from storage import queue_repository as queue_store
from version import APP_VERSION

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

_WORKER_ID: str | None = None


def _worker_identity() -> str:
    """Return a process-independent identity for this long-lived replica."""
    global _WORKER_ID
    if _WORKER_ID is None:
        configured = (
            os.getenv("INDEX_WORKER_ID", "").strip()
            or os.getenv("RAILWAY_REPLICA_ID", "").strip()
        )
        _WORKER_ID = configured or f"worker_{uuid.uuid4().hex}"
    return _WORKER_ID


def _heartbeat_interval_seconds() -> int:
    ttl_seconds = queue_store.worker_heartbeat_ttl_seconds()
    try:
        configured = int(
            os.getenv(
                "INDEX_WORKER_HEARTBEAT_INTERVAL_SECONDS",
                str(max(10, ttl_seconds // 3)),
            )
        )
    except (TypeError, ValueError):
        configured = max(10, ttl_seconds // 3)
    # Always refresh comfortably before expiry, even if deployment config is
    # accidentally larger than the TTL.
    return max(5, min(configured, max(5, ttl_seconds // 2)))


def _record_worker_heartbeat(queue_repo=None) -> dict:
    repository = queue_repo or queue_store.get_queue_repository()
    recorder = getattr(repository, "record_worker_heartbeat", None)
    if not callable(recorder):
        raise RuntimeError("queue backend does not support worker heartbeat")
    return recorder(_worker_identity(), APP_VERSION)


def _heartbeat_loop(stop_event: threading.Event, queue_repo) -> None:
    failed = False
    while not stop_event.is_set():
        try:
            _record_worker_heartbeat(queue_repo)
            if failed:
                print(json.dumps({
                    "maintenance": "worker_heartbeat",
                    "status": "recovered",
                    "version": APP_VERSION,
                }), flush=True)
            failed = False
        except Exception as exc:
            if not failed:
                print(json.dumps({
                    "maintenance": "worker_heartbeat",
                    "status": "failed",
                    "error": type(exc).__name__,
                }), flush=True)
            failed = True
        stop_event.wait(_heartbeat_interval_seconds())


def run_worker_once(timeout_seconds: int = 5) -> dict | None:
    return indexing_service.process_next_index_job(timeout_seconds=timeout_seconds)


def run_forever(timeout_seconds: int = 5, idle_sleep_seconds: float = 1.0) -> None:
    queue_repo = queue_store.get_queue_repository()
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(heartbeat_stop, queue_repo),
        name="index-worker-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    next_backfill_check = 0.0
    try:
        while True:
            if time.monotonic() >= next_backfill_check:
                from scripts.lightrag_backfill_worker import run_startup_backfill

                try:
                    backfill = run_startup_backfill()
                    if backfill.get("status") not in {"disabled", "already_done", "locked"}:
                        print(json.dumps({"maintenance": "lightrag_backfill", **backfill}, ensure_ascii=False), flush=True)
                except Exception as exc:
                    print(json.dumps({
                        "maintenance": "lightrag_backfill",
                        "status": "failed",
                        "error": type(exc).__name__,
                    }), flush=True)
                next_backfill_check = time.monotonic() + max(
                    60, int(os.getenv("LIGHTRAG_BACKFILL_INTERVAL_SECONDS", "300"))
                )
            result = run_worker_once(timeout_seconds=timeout_seconds)
            if result:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            else:
                time.sleep(idle_sleep_seconds)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GraphRAG Studio indexing worker.")
    parser.add_argument("--once", action="store_true", help="process one queued job and exit")
    parser.add_argument("--timeout", type=int, default=5, help="queue pop timeout in seconds")
    args = parser.parse_args()
    if args.once:
        print(json.dumps(run_worker_once(timeout_seconds=args.timeout), ensure_ascii=False, indent=2))
        return
    run_forever(timeout_seconds=args.timeout)


if __name__ == "__main__":
    main()
