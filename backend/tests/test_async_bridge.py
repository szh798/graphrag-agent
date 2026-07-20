from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services import async_bridge  # noqa: E402


class AsyncBridgeTests(unittest.TestCase):
    def tearDown(self):
        async_bridge.shutdown()

    def test_sequential_worker_calls_share_one_live_event_loop(self):
        bound_loop: asyncio.AbstractEventLoop | None = None
        loop_ids: list[int] = []

        async def use_loop_bound_client() -> int:
            nonlocal bound_loop
            current = asyncio.get_running_loop()
            if bound_loop is None:
                bound_loop = current
            elif bound_loop is not current:
                raise RuntimeError("client reused on a different event loop")
            await asyncio.sleep(0)
            loop_ids.append(id(current))
            return id(current)

        first = async_bridge.run(use_loop_bound_client())
        second = async_bridge.run(use_loop_bound_client())

        self.assertEqual(first, second)
        self.assertEqual(loop_ids, [first, first])

    def test_concurrent_sync_callers_are_serialized_onto_same_event_loop(self):
        caller_threads: set[int] = set()

        async def identify_runtime() -> tuple[int, int]:
            await asyncio.sleep(0.01)
            return id(asyncio.get_running_loop()), threading.get_ident()

        def invoke() -> tuple[int, int]:
            caller_threads.add(threading.get_ident())
            return async_bridge.run(identify_runtime())

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _index: invoke(), range(8)))

        self.assertGreater(len(caller_threads), 1)
        self.assertEqual(len({loop_id for loop_id, _thread_id in results}), 1)
        self.assertEqual(len({thread_id for _loop_id, thread_id in results}), 1)
        self.assertNotIn(results[0][1], caller_threads)

    def test_shutdown_allows_clean_process_runtime_restart(self):
        async def identity() -> tuple[int, int]:
            return id(asyncio.get_running_loop()), threading.get_ident()

        first = async_bridge.run(identity())
        async_bridge.shutdown()
        second = async_bridge.run(identity())

        # Thread identifiers can be recycled, but the loop object itself must
        # be new and live after an explicit shutdown.
        self.assertNotEqual(first[0], second[0])

    def test_after_fork_hook_discards_inherited_runtime(self):
        parent_bridge = async_bridge._get_bridge()
        try:
            async_bridge._after_fork_child()
            child_bridge = async_bridge._get_bridge()
            self.assertIsNot(parent_bridge, child_bridge)
            self.assertEqual(child_bridge.pid, parent_bridge.pid)
        finally:
            # In this test both simulated sides still exist in one process;
            # the real child has no inherited thread to stop.
            parent_bridge.close()


if __name__ == "__main__":
    unittest.main()
