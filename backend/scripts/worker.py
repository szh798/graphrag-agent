"""Long-running indexing worker entrypoint."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from dotenv import load_dotenv

from services import indexing_service

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def run_worker_once(timeout_seconds: int = 5) -> dict | None:
    return indexing_service.process_next_index_job(timeout_seconds=timeout_seconds)


def run_forever(timeout_seconds: int = 5, idle_sleep_seconds: float = 1.0) -> None:
    while True:
        result = run_worker_once(timeout_seconds=timeout_seconds)
        if result:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        else:
            time.sleep(idle_sleep_seconds)


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
