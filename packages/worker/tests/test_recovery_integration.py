"""
M1.5 — the acceptance test for the whole milestone, against a real Postgres.

M1's objective in ROADMAP.md: "A project and a job can be created via API and
survive a restart." The unit suites prove each piece; this proves the pieces
together, with real transactions, real row locks and a real enum:

  * create a project and a job through the API
  * a worker drives it, parks it at each gate, finishes it after approvals
  * kill the worker mid-job and restart: no state is lost, none is replayed
  * two workers racing one job cannot both advance it

Skipped unless RUN_DB_INTEGRATION=1 and a Postgres is reachable, matching
packages/db/tests/test_transitions_integration.py. CI runs it in the
`core-db-integration` job.
"""

from __future__ import annotations

import os
import threading

import pytest

from packages.db import migrate

pytestmark = [
    pytest.mark.db_integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION") != "1",
        reason="set RUN_DB_INTEGRATION=1 and provide a Postgres to enable",
    ),
]

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from packages.api.config import Settings  # noqa: E402
from packages.api.deps import Database  # noqa: E402
from packages.api.main import create_app  # noqa: E402
from packages.db.states import JobState as S  # noqa: E402
from packages.worker.queue import MemoryQueue  # noqa: E402
from packages.worker.runner import Worker  # noqa: E402

TOKEN = "integration-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _dsn() -> str:
    return os.getenv("DATABASE_URL", "")


@pytest.fixture()
def database():
    """A pool against a schema rebuilt from the migrations for each test."""
    dsn = _dsn()
    setup = psycopg.connect(dsn) if dsn else psycopg.connect()
    try:
        with setup.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        setup.commit()
        migrate.run(setup)
        setup.commit()
    finally:
        setup.close()

    db = Database(dsn, min_size=1, max_size=4)
    db.open()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def queue() -> MemoryQueue:
    return MemoryQueue()


@pytest.fixture()
def client(database, queue):
    settings = Settings(
        database_url=_dsn(), api_token=TOKEN, pool_min=1, pool_max=4,
        auto_migrate=False, redis_url="",
    )
    with TestClient(create_app(settings, database, queue)) as c:
        yield c


def _worker(queue, database, name="worker-1") -> Worker:
    return Worker(queue, database.connection, name=name, poll_timeout=0.2)


def _new_job(client) -> str:
    project = client.post(
        "/projects", json={"name": "Solar explainer"}, headers=AUTH
    ).json()
    return client.post(
        "/jobs", json={"project_id": project["id"]}, headers=AUTH
    ).json()["id"]


def _approve(client, job_id: str, gate: str) -> None:
    r = client.post(
        f"/jobs/{job_id}/gates/{gate}",
        json={"approved": True, "actor": "asad"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text


def _state(client, job_id: str) -> str:
    return client.get(f"/jobs/{job_id}", headers=AUTH).json()["state"]


# --- the acceptance path ----------------------------------------------------
def test_api_creates_a_project_and_a_job(client):
    job_id = _new_job(client)
    assert _state(client, job_id) == "draft"


def test_a_new_job_is_queued_for_the_worker(client, queue):
    job_id = _new_job(client)
    assert queue.reserve(timeout=0.2) == job_id


def test_the_worker_drives_the_job_to_the_first_gate(client, queue, database):
    job_id = _new_job(client)
    _worker(queue, database).run(max_jobs=10)
    body = client.get(f"/jobs/{job_id}", headers=AUTH).json()
    assert body["state"] == "planned"
    assert body["awaiting_gate"] == "g1_script"


def test_approvals_carry_the_job_all_the_way_to_complete(client, queue, database):
    job_id = _new_job(client)
    worker = _worker(queue, database)

    for gate, parked_at in (
        ("g1_script", "planned"),
        ("g2_storyboard", "retrieved"),
        ("g3_final", "review"),
    ):
        worker.run(max_jobs=10)
        assert _state(client, job_id) == parked_at
        _approve(client, job_id, gate)

    worker.run(max_jobs=10)
    assert _state(client, job_id) == "complete"


def test_the_finished_job_has_the_full_audit_trail(client, queue, database):
    job_id = _new_job(client)
    worker = _worker(queue, database)
    for gate in ("g1_script", "g2_storyboard", "g3_final"):
        worker.run(max_jobs=10)
        _approve(client, job_id, gate)
    worker.run(max_jobs=10)

    history = client.get(f"/jobs/{job_id}/history", headers=AUTH).json()
    moves = [
        (e["from_state"], e["to_state"])
        for e in history
        if e["event_type"] == "job.transition"
    ]
    assert moves == [
        ("draft", "planning"), ("planning", "planned"),
        ("planned", "retrieving"), ("retrieving", "retrieved"),
        ("retrieved", "rendering"), ("rendering", "rendered"),
        ("rendered", "qa"), ("qa", "review"), ("review", "complete"),
    ]
    decisions = [e for e in history if e["event_type"] == "approval.decision"]
    assert len(decisions) == 3


def test_a_finished_job_records_when_it_started_and_finished(client, queue, database):
    job_id = _new_job(client)
    worker = _worker(queue, database)
    for gate in ("g1_script", "g2_storyboard", "g3_final"):
        worker.run(max_jobs=10)
        _approve(client, job_id, gate)
    worker.run(max_jobs=10)

    body = client.get(f"/jobs/{job_id}", headers=AUTH).json()
    assert body["started_at"] is not None
    assert body["finished_at"] is not None


# --- restart recovery -------------------------------------------------------
def test_a_job_survives_a_worker_killed_mid_flight(client, queue, database):
    """The M1 acceptance criterion: restart mid-job, state is unchanged."""
    job_id = _new_job(client)
    _worker(queue, database).run(max_jobs=2)
    state_before = _state(client, job_id)

    # A worker reserves the job and dies: reserved, never acked.
    queue.push(job_id)
    assert queue.reserve(timeout=0.2) == job_id
    assert queue.depth() == (0, 1)

    assert _state(client, job_id) == state_before   # nothing rewound

    restarted = _worker(queue, database, name="worker-2")
    restarted.run(max_jobs=5)
    assert restarted.stats.recovered == 1
    assert _state(client, job_id) == "planned"


def test_a_restart_replays_no_transition(client, queue, database):
    job_id = _new_job(client)
    _worker(queue, database).run(max_jobs=10)
    before = client.get(f"/jobs/{job_id}/history", headers=AUTH).json()

    _worker(queue, database, name="worker-2").recover()
    after = client.get(f"/jobs/{job_id}/history", headers=AUTH).json()
    assert len(after) == len(before)


def test_a_redelivered_job_is_a_no_op_against_real_postgres(client, queue, database):
    job_id = _new_job(client)
    worker = _worker(queue, database)
    worker.run(max_jobs=10)          # parks at g1
    history_len = len(client.get(f"/jobs/{job_id}/history", headers=AUTH).json())

    worker.process(job_id)           # the same message, delivered again
    after = client.get(f"/jobs/{job_id}/history", headers=AUTH).json()
    assert len(after) == history_len


def test_the_api_survives_a_worker_that_is_not_running(client):
    """A job with no worker sits in draft. Nothing errors, nothing is lost."""
    job_id = _new_job(client)
    assert _state(client, job_id) == "draft"


# --- concurrency ------------------------------------------------------------
def test_two_workers_racing_one_job_produce_one_transition(client, queue, database):
    """The FOR UPDATE lock, under a real Postgres.

    Both workers are handed the same id deliberately — that is what a
    recovery sweep can do. Exactly one may advance it.
    """
    job_id = _new_job(client)
    queue.reserve(timeout=0.2)   # take the API's push off the queue

    barrier = threading.Barrier(2)
    outcomes: list = []
    lock = threading.Lock()

    def race(name: str):
        worker = Worker(MemoryQueue(), database.connection, name=name)
        barrier.wait(timeout=5)
        outcome = worker.process(job_id)
        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=race, args=(f"worker-{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(outcomes) == 2
    advanced = [o for o in outcomes if o.action == "advanced"]
    assert len(advanced) == 1, f"both workers advanced the job: {outcomes}"

    history = client.get(f"/jobs/{job_id}/history", headers=AUTH).json()
    moves = [e for e in history if e["event_type"] == "job.transition"]
    assert len(moves) == 1
    assert _state(client, job_id) == "planning"


# --- failure and retry ------------------------------------------------------
def test_an_operator_can_cancel_a_running_job(client, queue, database):
    job_id = _new_job(client)
    _worker(queue, database).run(max_jobs=2)

    r = client.post(
        f"/jobs/{job_id}/transition",
        json={"to": "cancelled", "actor": "asad"},
        headers=AUTH,
    )
    assert r.status_code == 200
    body = client.get(f"/jobs/{job_id}", headers=AUTH).json()
    assert body["state"] == "cancelled"
    assert body["terminal"] is True


def test_a_cancelled_job_is_left_alone_by_the_worker(client, queue, database):
    job_id = _new_job(client)
    client.post(
        f"/jobs/{job_id}/transition",
        json={"to": "cancelled", "actor": "asad"},
        headers=AUTH,
    )
    queue.push(job_id)
    worker = _worker(queue, database)
    worker.run(max_jobs=5)
    assert _state(client, job_id) == "cancelled"
    assert worker.stats.advanced == 0


def test_a_failed_job_can_be_retried_back_into_the_pipeline(client, queue, database):
    job_id = _new_job(client)
    _worker(queue, database).run(max_jobs=2)   # now in planning

    client.post(
        f"/jobs/{job_id}/transition",
        json={"to": "failed", "actor": "worker-1", "error": "provider timed out"},
        headers=AUTH,
    )
    assert client.get(f"/jobs/{job_id}", headers=AUTH).json()["error"] == (
        "provider timed out"
    )

    r = client.post(
        f"/jobs/{job_id}/transition",
        json={"to": "planning", "actor": "asad"},
        headers=AUTH,
    )
    assert r.status_code == 200
    body = client.get(f"/jobs/{job_id}", headers=AUTH).json()
    assert body["state"] == "planning"
    assert body["error"] is None     # the stale error is cleared

    _worker(queue, database).run(max_jobs=10)
    assert _state(client, job_id) == "planned"


def test_an_illegal_transition_is_refused_by_the_api(client):
    job_id = _new_job(client)
    r = client.post(
        f"/jobs/{job_id}/transition",
        json={"to": "complete", "actor": "asad"},
        headers=AUTH,
    )
    assert r.status_code == 409
    assert _state(client, job_id) == "draft"


def test_an_unapproved_gate_is_refused_by_the_api(client, queue, database):
    job_id = _new_job(client)
    _worker(queue, database).run(max_jobs=10)   # parked at g1

    r = client.post(
        f"/jobs/{job_id}/transition",
        json={"to": "retrieving", "actor": "asad"},
        headers=AUTH,
    )
    assert r.status_code == 403
    assert _state(client, job_id) == "planned"


def test_a_rejected_gate_leaves_the_job_parked(client, queue, database):
    job_id = _new_job(client)
    worker = _worker(queue, database)
    worker.run(max_jobs=10)

    client.post(
        f"/jobs/{job_id}/gates/g1_script",
        json={"approved": False, "actor": "asad", "note": "line 3 is wrong"},
        headers=AUTH,
    )
    queue.push(job_id)
    worker.run(max_jobs=5)
    assert _state(client, job_id) == "planned"

    _approve(client, job_id, "g1_script")
    worker.run(max_jobs=10)
    assert _state(client, job_id) == "retrieved"


# --- idempotency ------------------------------------------------------------
def test_an_idempotency_key_prevents_a_duplicate_production_run(client, queue):
    project = client.post(
        "/projects", json={"name": "Solar explainer"}, headers=AUTH
    ).json()
    body = {"project_id": project["id"], "idempotency_key": "nightly-2026-08-27"}

    first = client.post("/jobs", json=body, headers=AUTH)
    second = client.post("/jobs", json=body, headers=AUTH)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/jobs", headers=AUTH).json()) == 1
    assert queue.depth()[0] == 1
