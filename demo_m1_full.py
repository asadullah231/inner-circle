"""
M1 demo — the durable backend skeleton, end to end.

Runs the real API and the real worker against an in-memory database, so it
needs no Postgres, no Redis and no server:

    python demo_m1_full.py

What it shows, in order:
  1. A project and a job created through the HTTP API
  2. The worker driving the job until a human is needed
  3. Each of the three gates blocking, then releasing, the pipeline
  4. A rejection sending the job nowhere
  5. A worker killed mid-job, and a restart picking it up with nothing lost
  6. The rules that protect the pipeline, and the audit trail behind all of it

demo_m1.py is the smaller version: the state machine on its own, no API.
"""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient

from packages.api.config import Settings
from packages.api.main import create_app
from packages.api.tests.fakedb import FakeDatabase, Store
from packages.worker.queue import MemoryQueue
from packages.worker.runner import Worker

TTY = sys.stdout.isatty()
DIM = "\033[2m" if TTY else ""
BOLD = "\033[1m" if TTY else ""
GREEN = "\033[32m" if TTY else ""
RED = "\033[31m" if TTY else ""
YELLOW = "\033[33m" if TTY else ""
CYAN = "\033[36m" if TTY else ""
OFF = "\033[0m" if TTY else ""

TOKEN = "demo-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def head(n: int, text: str) -> None:
    print(f"\n{BOLD}{n}. {text}{OFF}")
    print(DIM + "-" * 66 + OFF)


def ok(text: str) -> None:
    print(f"   {GREEN}OK{OFF}      {text}")


def blocked(text: str) -> None:
    print(f"   {YELLOW}BLOCKED{OFF} {text}")


def refused(text: str) -> None:
    print(f"   {RED}REFUSED{OFF} {text}")


def call(text: str) -> None:
    print(f"   {CYAN}HTTP{OFF}    {text}")


class Demo:
    def __init__(self) -> None:
        self.store = Store()
        self.queue = MemoryQueue()
        self.db = FakeDatabase(self.store)
        settings = Settings(
            database_url="", api_token=TOKEN, pool_min=1, pool_max=2,
            auto_migrate=False, redis_url="",
        )
        self.client = TestClient(create_app(settings, self.db, self.queue))
        self.client.__enter__()

    def close(self) -> None:
        self.client.__exit__(None, None, None)

    # -- HTTP helpers --------------------------------------------------------
    def post(self, path: str, body: dict) -> dict:
        r = self.client.post(path, json=body, headers=AUTH)
        call(f"POST {path} -> {r.status_code}")
        return r.json()

    def get(self, path: str) -> dict:
        return self.client.get(path, headers=AUTH).json()

    def try_transition(self, job_id: str, to: str, actor: str) -> None:
        r = self.client.post(
            f"/jobs/{job_id}/transition", json={"to": to, "actor": actor}, headers=AUTH
        )
        if r.status_code == 200:
            changed = r.json()["changed"]
            if changed:
                ok(f"{actor}: job is now {BOLD}{to}{OFF}")
            else:
                print(f"   {CYAN}NO-OP{OFF}   already {to}, nothing written")
        elif r.status_code == 403:
            blocked(r.json()["detail"])
        elif r.status_code == 409:
            refused(r.json()["detail"])
        else:
            refused(f"{r.status_code}: {r.text}")

    def work(self, name: str = "worker-1") -> Worker:
        worker = Worker(self.queue, self.db.connection, name=name, poll_timeout=0.05)
        worker.run(max_jobs=30)
        return worker

    def state(self, job_id: str) -> str:
        return self.get(f"/jobs/{job_id}")["state"]


def main() -> None:
    print(f"\n{BOLD}INNER CIRCLE - M1 durable backend skeleton{OFF}")
    print(DIM + "FastAPI + job queue + worker + state machine, running for real" + OFF)

    demo = Demo()
    try:
        run(demo)
    finally:
        demo.close()


def run(d: Demo) -> None:
    # 1 ---------------------------------------------------------------------
    head(1, "A project and a job, created through the API")
    project = d.post("/projects", {"name": "Solar panels explainer"})
    job = d.post("/jobs", {"project_id": project["id"]})
    job_id = job["id"]
    print(f"\n   project : {project['name']}")
    print(f"   job     : {job_id[:8]}")
    print(f"   state   : {job['state']}")
    print(f"   {DIM}the API queued it; no worker has touched it yet{OFF}")

    # 2 ---------------------------------------------------------------------
    head(2, "The worker runs until it needs a human")
    worker = d.work()
    ok(f"worker advanced the job {worker.stats.advanced} times, then parked it")
    body = d.get(f"/jobs/{job_id}")
    blocked(f"state {BOLD}{body['state']}{OFF}, waiting on {BOLD}{body['awaiting_gate']}{OFF}")
    print(f"   {DIM}nothing is polling. the job sits here until someone decides.{OFF}")

    # 3 ---------------------------------------------------------------------
    head(3, "G1: a rejection sends it nowhere, an approval releases it")
    d.post(
        f"/jobs/{job_id}/gates/g1_script",
        {"approved": False, "actor": "asad", "note": "line 3 is factually wrong"},
    )
    print(f"   {YELLOW}REJECTED{OFF} g1_script - a rejection requires a note, enforced in code")
    d.work()
    print(f"   {DIM}worker looked again: still {d.state(job_id)}{OFF}")

    print(f"\n   {DIM}script fixed, reviewer approves{OFF}")
    d.post(f"/jobs/{job_id}/gates/g1_script", {"approved": True, "actor": "asad"})
    ok("asad approved g1_script - the API put the job back on the queue")
    d.work()
    body = d.get(f"/jobs/{job_id}")
    blocked(f"state {BOLD}{body['state']}{OFF}, waiting on {BOLD}{body['awaiting_gate']}{OFF}")

    # 4 ---------------------------------------------------------------------
    head(4, "G2 and G3 carry it to the end")
    for gate in ("g2_storyboard", "g3_final"):
        d.post(f"/jobs/{job_id}/gates/{gate}", {"approved": True, "actor": "asad"})
        ok(f"asad approved {gate}")
        d.work()
        body = d.get(f"/jobs/{job_id}")
        label = body["awaiting_gate"] or "-"
        print(f"   {DIM}state {body['state']}, waiting on {label}{OFF}")
    ok(f"job is {BOLD}{d.state(job_id)}{OFF}")

    # 5 ---------------------------------------------------------------------
    head(5, "A worker killed mid-job loses nothing")
    second = d.post("/jobs", {"project_id": project["id"]})
    second_id = second["id"]
    d.work()                                   # runs to the first gate
    parked_at = d.state(second_id)
    print(f"   {DIM}job {second_id[:8]} is parked at {parked_at}{OFF}")

    d.post(f"/jobs/{second_id}/gates/g1_script", {"approved": True, "actor": "asad"})
    d.queue.reserve(timeout=0.1)               # a worker took it...
    print(f"   {RED}CRASH{OFF}   worker died holding the job, never acked it")
    print(f"   {DIM}queue: {d.queue.depth()[0]} pending, {d.queue.depth()[1]} in flight{OFF}")
    print(f"   {DIM}state in the database is still {d.state(second_id)} - nothing rewound{OFF}")

    restarted = d.work(name="worker-2")
    ok(f"worker-2 recovered {restarted.stats.recovered} in-flight job on startup")
    print(f"   {DIM}and carried it on to {d.state(second_id)}{OFF}")

    # 6 ---------------------------------------------------------------------
    head(6, "The rules that protect the pipeline")

    print(f"   {DIM}a finished job cannot be restarted{OFF}")
    d.try_transition(job_id, "planning", "operator")

    print(f"\n   {DIM}a fresh job cannot skip to the end{OFF}")
    third = d.post("/jobs", {"project_id": project["id"]})
    d.try_transition(third["id"], "complete", "operator")

    print(f"\n   {DIM}a redelivered worker message is a no-op, not a crash{OFF}")
    d.try_transition(third["id"], "draft", "worker-1")

    print(f"\n   {DIM}an ungated job cannot be pushed past a gate{OFF}")
    d.work()
    d.try_transition(third["id"], "retrieving", "operator")

    print(f"\n   {DIM}a failed job is retried, not thrown away{OFF}")
    d.try_transition(third["id"], "failed", "worker-1")
    d.try_transition(third["id"], "planning", "operator")

    # 7 ---------------------------------------------------------------------
    head(7, "Audit trail - every move, who made it")
    for event in d.get(f"/jobs/{job_id}/history"):
        stamp = str(event["created_at"])[11:19]
        if event["event_type"] == "job.transition":
            what = f"{event['from_state']} -> {event['to_state']}"
        else:
            detail = event["detail"] or {}
            what = f"{detail.get('gate')} {detail.get('decision')}"
        print(f"   {DIM}{stamp}{OFF}  {what:<28} {DIM}{event['actor']}{OFF}")

    print(f"\n{DIM}   Every line above is a row in audit_events. Nothing moves this")
    print(f"   pipeline without leaving one.{OFF}")

    print(f"\n{BOLD}   final state: {GREEN}{d.state(job_id)}{OFF}")
    print(f"{DIM}   M1 is closed. Next: M2 puts a real planner behind the")
    print(f"   planning step, in place of the stub the worker runs today.{OFF}\n")


if __name__ == "__main__":
    main()
