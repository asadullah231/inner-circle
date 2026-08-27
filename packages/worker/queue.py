"""
Job queue backed by a Redis list.

Deliberately small. A full broker (Celery, RQ, arq) buys retry policies and a
result backend we do not need: the job's state already lives in Postgres, and
the state machine already makes redelivery safe. What we need from the queue is
only "hand a job id to one worker at a time, and do not lose it if that worker
dies".

Delivery model:
  * `push`   -> LPUSH onto the pending list (reserve takes from the other end)
  * `reserve`→ BLMOVE pending -> processing (atomic, blocking)
  * `ack`    → LREM from processing
  * `recover`→ move stale processing entries back to pending

BLMOVE is what makes a crash survivable: the id sits in `processing` until the
worker acks it, so a killed worker leaves the job visible rather than losing
it. `recover()` is the sweep that puts it back, and M1.5 tests exactly that.

An in-memory implementation lives alongside it so the worker can be tested,
and run locally, without Redis.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional, Protocol

log = logging.getLogger("worker.queue")

DEFAULT_PENDING = "ic:jobs:pending"
DEFAULT_PROCESSING = "ic:jobs:processing"


class Queue(Protocol):
    """What the worker needs. Both implementations below satisfy it."""

    def push(self, job_id: str) -> None: ...
    def reserve(self, timeout: float = 5.0) -> Optional[str]: ...
    def ack(self, job_id: str) -> None: ...
    def requeue(self, job_id: str) -> None: ...
    def recover(self) -> list[str]: ...
    def depth(self) -> tuple[int, int]: ...


class MemoryQueue:
    """Thread-safe in-process queue. Used by the tests and by `--dry-run`."""

    def __init__(self) -> None:
        self._pending: deque[str] = deque()
        self._processing: list[str] = []
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)

    def push(self, job_id: str) -> None:
        with self._not_empty:
            self._pending.appendleft(job_id)
            self._not_empty.notify()

    def reserve(self, timeout: float = 5.0) -> Optional[str]:
        deadline = time.monotonic() + timeout
        with self._not_empty:
            while not self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._not_empty.wait(remaining)
            job_id = self._pending.pop()
            self._processing.append(job_id)
            return job_id

    def ack(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._processing:
                self._processing.remove(job_id)

    def requeue(self, job_id: str) -> None:
        with self._not_empty:
            if job_id in self._processing:
                self._processing.remove(job_id)
            self._pending.append(job_id)   # reserve() pops the right, so this is next
            self._not_empty.notify()

    def recover(self) -> list[str]:
        with self._not_empty:
            stale = list(self._processing)
            self._processing.clear()
            for job_id in reversed(stale):
                self._pending.appendleft(job_id)
            if stale:
                self._not_empty.notify_all()
        if stale:
            log.warning("recovered %d in-flight job(s) after restart", len(stale))
        return stale

    def depth(self) -> tuple[int, int]:
        with self._lock:
            return len(self._pending), len(self._processing)


class RedisQueue:
    """Redis-backed queue. redis-py is imported lazily so nothing else in the
    package needs the dependency installed."""

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        pending_key: str = DEFAULT_PENDING,
        processing_key: str = DEFAULT_PROCESSING,
        client: Optional[object] = None,
    ):
        self.pending_key = pending_key
        self.processing_key = processing_key
        if client is not None:
            self._r = client
        else:
            import redis

            self._r = redis.Redis.from_url(url, decode_responses=True)

    def push(self, job_id: str) -> None:
        self._r.lpush(self.pending_key, job_id)

    def reserve(self, timeout: float = 5.0) -> Optional[str]:
        # BLMOVE is atomic: the id is never in neither list, so a worker that
        # dies between the two lists cannot exist.
        return self._r.blmove(
            self.pending_key, self.processing_key, timeout, "RIGHT", "LEFT"
        )

    def ack(self, job_id: str) -> None:
        self._r.lrem(self.processing_key, 1, job_id)

    def requeue(self, job_id: str) -> None:
        # RPUSH, not LPUSH: reserve() takes from the right, so this is next.
        self._r.lrem(self.processing_key, 1, job_id)
        self._r.rpush(self.pending_key, job_id)

    def recover(self) -> list[str]:
        """Move everything left in `processing` back to `pending`.

        Called at worker startup. With one worker this is exactly the set a
        crash orphaned. With several, run it from a single operator command
        rather than on every boot, or a live worker's job gets duplicated —
        which the state machine tolerates, but which wastes a run.
        """
        moved: list[str] = []
        while True:
            job_id = self._r.rpoplpush(self.processing_key, self.pending_key)
            if job_id is None:
                break
            moved.append(job_id)
        if moved:
            log.warning("recovered %d in-flight job(s) after restart", len(moved))
        return moved

    def depth(self) -> tuple[int, int]:
        return int(self._r.llen(self.pending_key)), int(self._r.llen(self.processing_key))
