"""
Queue tests.

MemoryQueue is tested directly. RedisQueue is tested against a fake client
that records the commands issued, because the guarantee that matters is
*which* Redis command is used — BLMOVE rather than BRPOP is the whole reason a
crashed worker does not lose a job.
"""

from __future__ import annotations

import threading

import pytest

from packages.worker.queue import MemoryQueue, RedisQueue


# --- MemoryQueue ------------------------------------------------------------
def test_push_then_reserve_returns_the_job():
    q = MemoryQueue()
    q.push("job-1")
    assert q.reserve(timeout=0.1) == "job-1"


def test_reserve_on_an_empty_queue_returns_none_after_the_timeout():
    assert MemoryQueue().reserve(timeout=0.05) is None


def test_reserved_jobs_move_to_processing_until_acked():
    q = MemoryQueue()
    q.push("job-1")
    q.reserve(timeout=0.1)
    assert q.depth() == (0, 1)
    q.ack("job-1")
    assert q.depth() == (0, 0)


def test_jobs_come_back_in_the_order_they_were_pushed():
    q = MemoryQueue()
    for i in range(3):
        q.push(f"job-{i}")
    assert [q.reserve(timeout=0.1) for _ in range(3)] == ["job-0", "job-1", "job-2"]


def test_two_reservers_never_get_the_same_job():
    q = MemoryQueue()
    for i in range(20):
        q.push(f"job-{i}")
    seen: list[str] = []
    lock = threading.Lock()

    def drain():
        while True:
            job = q.reserve(timeout=0.2)
            if job is None:
                return
            with lock:
                seen.append(job)

    threads = [threading.Thread(target=drain) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(seen) == sorted(f"job-{i}" for i in range(20))
    assert len(seen) == len(set(seen))


def test_recover_returns_unacked_jobs_to_pending():
    """The crash case: the worker died holding the job, never acked."""
    q = MemoryQueue()
    q.push("job-1")
    q.reserve(timeout=0.1)
    assert q.depth() == (0, 1)

    assert q.recover() == ["job-1"]
    assert q.depth() == (1, 0)
    assert q.reserve(timeout=0.1) == "job-1"


def test_recover_on_a_clean_queue_does_nothing():
    q = MemoryQueue()
    q.push("job-1")
    q.reserve(timeout=0.1)
    q.ack("job-1")
    assert q.recover() == []


def test_requeue_puts_a_job_back_at_the_head_of_the_line():
    """A retried job goes next, not behind everything already waiting."""
    q = MemoryQueue()
    q.push("job-1")
    q.push("job-2")
    assert q.reserve(timeout=0.1) == "job-1"
    q.requeue("job-1")
    assert q.reserve(timeout=0.1) == "job-1"


def test_redis_requeue_puts_a_job_back_at_the_head_of_the_line():
    fake = FakeRedis()
    q = RedisQueue(client=fake)
    q.push("job-1")
    q.push("job-2")
    assert q.reserve(timeout=1) == "job-1"
    q.requeue("job-1")
    assert q.reserve(timeout=1) == "job-1"


def test_ack_of_an_unknown_job_is_harmless():
    MemoryQueue().ack("never-seen")


# --- RedisQueue -------------------------------------------------------------
class FakeRedis:
    """Just enough of redis-py to record what the queue asks for."""

    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.calls: list[tuple] = []

    def lpush(self, key, value):
        self.calls.append(("lpush", key, value))
        self.lists.setdefault(key, []).insert(0, value)

    def rpush(self, key, value):
        self.calls.append(("rpush", key, value))
        self.lists.setdefault(key, []).append(value)

    def blmove(self, src, dst, timeout, src_side, dst_side):
        self.calls.append(("blmove", src, dst, timeout, src_side, dst_side))
        items = self.lists.get(src, [])
        if not items:
            return None
        value = items.pop() if src_side == "RIGHT" else items.pop(0)
        target = self.lists.setdefault(dst, [])
        target.insert(0, value) if dst_side == "LEFT" else target.append(value)
        return value

    def lrem(self, key, count, value):
        self.calls.append(("lrem", key, count, value))
        items = self.lists.get(key, [])
        if value in items:
            items.remove(value)

    def rpoplpush(self, src, dst):
        self.calls.append(("rpoplpush", src, dst))
        items = self.lists.get(src, [])
        if not items:
            return None
        value = items.pop()
        self.lists.setdefault(dst, []).insert(0, value)
        return value

    def llen(self, key):
        return len(self.lists.get(key, []))


@pytest.fixture()
def redis_queue():
    fake = FakeRedis()
    return RedisQueue(client=fake), fake


def test_redis_push_uses_lpush(redis_queue):
    q, fake = redis_queue
    q.push("job-1")
    assert fake.calls == [("lpush", "ic:jobs:pending", "job-1")]


def test_redis_reserve_uses_blmove_not_a_destructive_pop(redis_queue):
    """BRPOP would drop the job if the worker died before finishing it."""
    q, fake = redis_queue
    q.push("job-1")
    assert q.reserve(timeout=3) == "job-1"
    command = fake.calls[-1]
    assert command[0] == "blmove"
    assert command[1:3] == ("ic:jobs:pending", "ic:jobs:processing")


def test_redis_reserve_leaves_the_job_in_processing(redis_queue):
    q, fake = redis_queue
    q.push("job-1")
    q.reserve(timeout=1)
    assert fake.lists["ic:jobs:processing"] == ["job-1"]
    assert fake.lists["ic:jobs:pending"] == []


def test_redis_ack_removes_it_from_processing(redis_queue):
    q, fake = redis_queue
    q.push("job-1")
    q.reserve(timeout=1)
    q.ack("job-1")
    assert fake.lists["ic:jobs:processing"] == []


def test_redis_reserve_returns_none_when_empty(redis_queue):
    q, _ = redis_queue
    assert q.reserve(timeout=1) is None


def test_redis_recover_drains_processing_back_to_pending(redis_queue):
    q, fake = redis_queue
    for i in range(3):
        q.push(f"job-{i}")
        q.reserve(timeout=1)
    assert q.recover() == ["job-0", "job-1", "job-2"]
    assert fake.lists["ic:jobs:processing"] == []
    assert len(fake.lists["ic:jobs:pending"]) == 3


def test_redis_recover_on_a_clean_queue_returns_nothing(redis_queue):
    q, _ = redis_queue
    assert q.recover() == []


def test_redis_depth_reports_both_lists(redis_queue):
    q, _ = redis_queue
    q.push("a")
    q.push("b")
    q.reserve(timeout=1)
    assert q.depth() == (1, 1)


def test_redis_queue_keys_are_configurable(redis_queue):
    fake = FakeRedis()
    q = RedisQueue(client=fake, pending_key="alt:pending", processing_key="alt:proc")
    q.push("job-1")
    assert "alt:pending" in fake.lists
