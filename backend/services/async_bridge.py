"""Run async engine calls from synchronous workers on one process event loop.

The Railway index worker is intentionally synchronous at its queue boundary,
while embedded LightRAG and its Postgres/Neo4j clients are asynchronous.  A
fresh ``asyncio.run`` for every queue item binds cached clients and locks to a
loop that is immediately closed.  Reusing those objects on the next job then
fails with cross-loop or closed-loop errors.

This module owns one daemon-thread event loop per process and submits every
synchronous-to-async call with ``run_coroutine_threadsafe``.  The bridge is
discarded after ``fork`` (threads do not survive a fork) and shut down at normal
process exit.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar


T = TypeVar("T")


class _ProcessEventLoopBridge:
    def __init__(self) -> None:
        self.pid = os.getpid()
        self._ready = threading.Event()
        self._state_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._startup_error: BaseException | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run_loop,
            name="lightrag-async-runtime",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            raise RuntimeError("Failed to start the async runtime") from self._startup_error

    def _run_loop(self) -> None:
        loop: asyncio.AbstractEventLoop | None = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        except BaseException as exc:  # pragma: no cover - interpreter/runtime failure
            self._startup_error = exc
            self._ready.set()
            return

        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            asyncio.set_event_loop(None)
            loop.close()

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> Future[T]:
        if os.getpid() != self.pid:
            coroutine.close()
            raise RuntimeError("The async runtime cannot be reused after fork")
        if threading.current_thread() is self._thread:
            coroutine.close()
            raise RuntimeError("The async runtime cannot synchronously call itself")
        with self._state_lock:
            loop = self._loop
            if self._closed or loop is None or not loop.is_running():
                coroutine.close()
                raise RuntimeError("The async runtime is not running")
            return asyncio.run_coroutine_threadsafe(coroutine, loop)

    def close(self) -> None:
        # In a forked child the inherited thread no longer exists.  The child
        # reset hook drops the object, but this guard also makes direct cleanup
        # safe if close races with process initialisation.
        if os.getpid() != self.pid:
            return
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)


_bridge: _ProcessEventLoopBridge | None = None
_bridge_lock = threading.RLock()


def _get_bridge() -> _ProcessEventLoopBridge:
    global _bridge
    pid = os.getpid()
    with _bridge_lock:
        if _bridge is None or _bridge.pid != pid:
            # A mismatched bridge can only be inherited from a fork; its
            # daemon thread does not exist in this process and must not be
            # joined or stopped here.
            _bridge = _ProcessEventLoopBridge()
        return _bridge


def run(coroutine: Coroutine[Any, Any, T], *, timeout: float | None = None) -> T:
    """Synchronously wait for ``coroutine`` on the process-owned event loop."""

    try:
        bridge = _get_bridge()
    except BaseException:
        coroutine.close()
        raise
    return bridge.submit(coroutine).result(timeout=timeout)


def shutdown() -> None:
    """Stop the process bridge; the next call to :func:`run` starts a new one."""

    global _bridge
    with _bridge_lock:
        bridge = _bridge
        _bridge = None
    if bridge is not None:
        bridge.close()


def _after_fork_child() -> None:
    global _bridge, _bridge_lock
    # Locks and threads may have been mid-operation in the parent.  Replacing
    # both objects is required to avoid a permanently locked child process.
    _bridge = None
    _bridge_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)
atexit.register(shutdown)
