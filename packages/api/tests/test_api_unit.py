"""
API tests against the in-memory database in fakedb.py.

These cover the HTTP contract: status codes, auth, validation, and — the part
that matters — that an endpoint cannot move a job in a way the state machine
forbids. test_api_integration.py repeats the important paths against a real
Postgres.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from packages.api.config import Settings  # noqa: E402
from packages.api.main import create_app  # noqa: E402
from packages.api.tests.fakedb import FakeDatabase, Store  # noqa: E402
from packages.worker.queue import MemoryQueue  # noqa: E402

TOKEN = "test-token-9f2c"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _settings(**over) -> Settings:
    base = dict(
        database_url="", api_token=TOKEN, pool_min=1, pool_max=2,
        auto_migrate=False, redis_url="",
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture()
def store() -> Store:
    return Store()


@pytest.fixture()
def queue() -> MemoryQueue:
    return MemoryQueue()


@pytest.fixture()
def client(store, queue):
    db = FakeDatabase(store)
    with TestClient(create_app(_settings(), db, queue)) as c:
        yield c


@pytest.fixture()
def open_client(store, queue):
    """No API_TOKEN configured — auth disabled, as in local dev."""
    db = FakeDatabase(store)
    with TestClient(create_app(_settings(api_token=""), db, queue)) as c:
        yield c


# --- health -----------------------------------------------------------------
def test_health_needs_no_token(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"
    assert body["auth"] == "enabled"


def test_health_reports_degraded_when_database_is_down(client, store):
    store.fail_ping = True
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["database"] == "down"


def test_health_reports_when_auth_is_disabled(open_client):
    assert open_client.get("/health").json()["auth"] == "disabled"


# --- auth -------------------------------------------------------------------
def test_endpoints_reject_a_missing_token(client):
    r = client.get("/projects")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_endpoints_reject_a_wrong_token(client):
    r = client.get("/projects", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_endpoints_reject_a_non_bearer_scheme(client):
    r = client.get("/projects", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


def test_auth_disabled_lets_a_request_through(open_client):
    assert open_client.get("/projects").status_code == 200


# --- projects ---------------------------------------------------------------
def test_create_project_returns_201_and_the_row(client):
    r = client.post("/projects", json={"name": "Solar explainer"}, headers=AUTH)
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Solar explainer"
    assert (body["format_w"], body["format_h"], body["fps"]) == (1080, 1920, 30)


def test_create_project_stamps_the_caller_as_creator(client, store):
    client.post("/projects", json={"name": "P"}, headers=AUTH)
    assert next(iter(store.projects.values()))["created_by"] == "api-token"


def test_create_project_rejects_a_blank_name(client):
    assert client.post("/projects", json={"name": ""}, headers=AUTH).status_code == 422


def test_create_project_rejects_an_absurd_fps(client):
    r = client.post("/projects", json={"name": "P", "fps": 9000}, headers=AUTH)
    assert r.status_code == 422


def test_get_project_returns_404_for_an_unknown_id(client):
    assert client.get(f"/projects/{uuid4()}", headers=AUTH).status_code == 404


def test_list_projects_returns_what_was_created(client, store):
    store.add_project(name="A")
    store.add_project(name="B")
    names = {p["name"] for p in client.get("/projects", headers=AUTH).json()}
    assert names == {"A", "B"}


def test_patch_project_updates_only_the_supplied_field(client, store):
    p = store.add_project(name="Old", fps=30)
    r = client.patch(f"/projects/{p['id']}", json={"name": "New"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["name"] == "New"
    assert r.json()["fps"] == 30


def test_delete_project_returns_204_then_404(client, store):
    p = store.add_project()
    assert client.delete(f"/projects/{p['id']}", headers=AUTH).status_code == 204
    assert client.get(f"/projects/{p['id']}", headers=AUTH).status_code == 404


def test_delete_unknown_project_is_404(client):
    assert client.delete(f"/projects/{uuid4()}", headers=AUTH).status_code == 404


# --- jobs -------------------------------------------------------------------
def test_create_job_starts_in_draft(client, store):
    p = store.add_project()
    r = client.post("/jobs", json={"project_id": p["id"]}, headers=AUTH)
    assert r.status_code == 201
    body = r.json()
    assert body["state"] == "draft"
    assert body["terminal"] is False
    assert body["awaiting_gate"] is None


def test_create_job_for_an_unknown_project_is_404(client):
    r = client.post("/jobs", json={"project_id": str(uuid4())}, headers=AUTH)
    assert r.status_code == 404


def test_replaying_an_idempotency_key_returns_the_same_job_with_200(client, store):
    p = store.add_project()
    body = {"project_id": p["id"], "idempotency_key": "batch-42"}
    first = client.post("/jobs", json=body, headers=AUTH)
    second = client.post("/jobs", json=body, headers=AUTH)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(store.jobs) == 1


def test_get_job_reports_the_gate_it_is_parked_on(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="planned")
    body = client.get(f"/jobs/{j['id']}", headers=AUTH).json()
    assert body["awaiting_gate"] == "g1_script"


def test_get_job_marks_a_terminal_job(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="complete")
    assert client.get(f"/jobs/{j['id']}", headers=AUTH).json()["terminal"] is True


def test_get_unknown_job_is_404(client):
    assert client.get(f"/jobs/{uuid4()}", headers=AUTH).status_code == 404


def test_list_jobs_filters_by_state(client, store):
    p = store.add_project()
    store.add_job(p["id"], state="draft")
    store.add_job(p["id"], state="planning")
    rows = client.get("/jobs?state=planning", headers=AUTH).json()
    assert [r["state"] for r in rows] == ["planning"]


def test_list_jobs_filters_by_project(client, store):
    a, b = store.add_project(), store.add_project()
    store.add_job(a["id"])
    store.add_job(b["id"])
    rows = client.get(f"/jobs?project_id={a['id']}", headers=AUTH).json()
    assert [r["project_id"] for r in rows] == [a["id"]]


def test_list_jobs_rejects_an_unknown_state(client):
    assert client.get("/jobs?state=nonsense", headers=AUTH).status_code == 422


# --- gates ------------------------------------------------------------------
def test_approving_a_gate_records_the_decision(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="planned")
    r = client.post(
        f"/jobs/{j['id']}/gates/g1_script",
        json={"approved": True, "actor": "asad"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert store.approvals[(j["id"], "g1_script")]["decision"] == "approved"


def test_approving_a_gate_does_not_advance_the_job(client, store):
    """PRD FR-3: approval unblocks the worker, it does not do the worker's job."""
    p = store.add_project()
    j = store.add_job(p["id"], state="planned")
    body = client.post(
        f"/jobs/{j['id']}/gates/g1_script",
        json={"approved": True, "actor": "asad"},
        headers=AUTH,
    ).json()
    assert body["state"] == "planned"
    assert body["approvals"][0]["decision"] == "approved"


def test_rejecting_a_gate_without_a_note_is_422(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="planned")
    r = client.post(
        f"/jobs/{j['id']}/gates/g1_script",
        json={"approved": False, "actor": "asad"},
        headers=AUTH,
    )
    assert r.status_code == 422
    assert "note" in r.json()["detail"]


def test_rejecting_a_gate_with_a_note_is_recorded(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="planned")
    r = client.post(
        f"/jobs/{j['id']}/gates/g1_script",
        json={"approved": False, "actor": "asad", "note": "line 3 is wrong"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert store.approvals[(j["id"], "g1_script")]["decision"] == "rejected"


def test_gate_on_an_unknown_job_is_404(client):
    r = client.post(
        f"/jobs/{uuid4()}/gates/g1_script",
        json={"approved": True, "actor": "asad"},
        headers=AUTH,
    )
    assert r.status_code == 404


def test_an_unknown_gate_name_is_422(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="planned")
    r = client.post(
        f"/jobs/{j['id']}/gates/g9_nonsense",
        json={"approved": True, "actor": "asad"},
        headers=AUTH,
    )
    assert r.status_code == 422


# --- transitions ------------------------------------------------------------
def test_a_legal_transition_moves_the_job(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="draft")
    r = client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "planning", "actor": "operator"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["changed"] is True
    assert store.jobs[j["id"]]["state"] == "planning"


def test_an_illegal_transition_is_409_and_writes_nothing(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="draft")
    r = client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "complete", "actor": "operator"},
        headers=AUTH,
    )
    assert r.status_code == 409
    assert "illegal transition" in r.json()["detail"]
    assert store.jobs[j["id"]]["state"] == "draft"
    assert store.audit == []


def test_an_ungated_transition_is_403(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="planned")
    r = client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "retrieving", "actor": "operator"},
        headers=AUTH,
    )
    assert r.status_code == 403
    assert "g1_script" in r.json()["detail"]
    assert store.jobs[j["id"]]["state"] == "planned"


def test_approval_then_transition_passes_the_gate(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="planned")
    client.post(
        f"/jobs/{j['id']}/gates/g1_script",
        json={"approved": True, "actor": "asad"},
        headers=AUTH,
    )
    r = client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "retrieving", "actor": "worker-1"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert store.jobs[j["id"]]["state"] == "retrieving"


def test_a_repeated_transition_is_a_no_op_not_an_error(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="planning")
    r = client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "planning", "actor": "worker-1"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert store.audit == []


def test_a_transition_locks_the_job_row(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="draft")
    client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "planning", "actor": "operator"},
        headers=AUTH,
    )
    assert store.locked == [j["id"]]


def test_transition_on_an_unknown_job_is_404(client):
    r = client.post(
        f"/jobs/{uuid4()}/transition",
        json={"to": "planning", "actor": "operator"},
        headers=AUTH,
    )
    assert r.status_code == 404


def test_failing_a_job_records_the_error(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="planning")
    client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "failed", "actor": "worker-1", "error": "provider timed out"},
        headers=AUTH,
    )
    assert store.jobs[j["id"]]["state"] == "failed"
    assert store.jobs[j["id"]]["error"] == "provider timed out"
    assert store.jobs[j["id"]]["finished_at"] is not None


def test_a_failed_job_can_be_retried_back_into_the_pipeline(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="failed", error="provider timed out")
    r = client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "planning", "actor": "operator"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert store.jobs[j["id"]]["state"] == "planning"
    assert store.jobs[j["id"]]["error"] is None


def test_a_complete_job_cannot_be_restarted(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="complete")
    r = client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "planning", "actor": "operator"},
        headers=AUTH,
    )
    assert r.status_code == 409
    assert "terminal" in r.json()["detail"]


# --- history ----------------------------------------------------------------
def test_history_returns_every_transition_in_order(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="draft")
    for to in ("planning", "planned"):
        client.post(
            f"/jobs/{j['id']}/transition",
            json={"to": to, "actor": "worker-1"},
            headers=AUTH,
        )
    rows = client.get(f"/jobs/{j['id']}/history", headers=AUTH).json()
    assert [(r["from_state"], r["to_state"]) for r in rows] == [
        ("draft", "planning"),
        ("planning", "planned"),
    ]
    assert all(r["actor"] == "worker-1" for r in rows)


def test_history_includes_gate_decisions(client, store):
    p = store.add_project()
    j = store.add_job(p["id"], state="planned")
    client.post(
        f"/jobs/{j['id']}/gates/g1_script",
        json={"approved": True, "actor": "asad"},
        headers=AUTH,
    )
    rows = client.get(f"/jobs/{j['id']}/history", headers=AUTH).json()
    assert rows[0]["event_type"] == "approval.decision"
    assert rows[0]["detail"]["gate"] == "g1_script"


def test_history_on_an_unknown_job_is_404(client):
    assert client.get(f"/jobs/{uuid4()}/history", headers=AUTH).status_code == 404


# --- transaction boundary ---------------------------------------------------
def test_a_successful_request_commits_once(client, store):
    client.post("/projects", json={"name": "P"}, headers=AUTH)
    assert store.commits == 1
    assert store.rollbacks == 0


def test_a_rejected_transition_rolls_back(client, store):
    """The 409 must leave nothing behind, including the FOR UPDATE lock."""
    p = store.add_project()
    j = store.add_job(p["id"], state="draft")
    store.commits = store.rollbacks = 0
    client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "complete", "actor": "operator"},
        headers=AUTH,
    )
    assert store.rollbacks == 1
    assert store.commits == 0


# --- contract ---------------------------------------------------------------
def test_openapi_schema_builds(client):
    """A broken response_model only surfaces when the schema is generated."""
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    for route in ("/health", "/projects", "/jobs", "/jobs/{job_id}/transition"):
        assert route in paths


# --- queue wiring -----------------------------------------------------------
def test_creating_a_job_queues_it_for_the_worker(client, store, queue):
    p = store.add_project()
    r = client.post("/jobs", json={"project_id": p["id"]}, headers=AUTH)
    assert queue.reserve(timeout=0.1) == r.json()["id"]


def test_a_replayed_idempotency_key_does_not_queue_the_job_twice(client, store, queue):
    p = store.add_project()
    body = {"project_id": p["id"], "idempotency_key": "batch-1"}
    client.post("/jobs", json=body, headers=AUTH)
    client.post("/jobs", json=body, headers=AUTH)
    assert queue.depth()[0] == 1


def test_approving_a_gate_puts_the_job_back_on_the_queue(client, store, queue):
    """This is what stops a parked job needing a poll loop."""
    p = store.add_project()
    j = store.add_job(p["id"], state="planned")
    client.post(
        f"/jobs/{j['id']}/gates/g1_script",
        json={"approved": True, "actor": "asad"},
        headers=AUTH,
    )
    assert queue.reserve(timeout=0.1) == j["id"]


def test_rejecting_a_gate_does_not_queue_the_job(client, store, queue):
    p = store.add_project()
    j = store.add_job(p["id"], state="planned")
    client.post(
        f"/jobs/{j['id']}/gates/g1_script",
        json={"approved": False, "actor": "asad", "note": "wrong"},
        headers=AUTH,
    )
    assert queue.depth() == (0, 0)


def test_an_operator_retry_re_queues_the_job(client, store, queue):
    p = store.add_project()
    j = store.add_job(p["id"], state="failed")
    client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "planning", "actor": "operator"},
        headers=AUTH,
    )
    assert queue.reserve(timeout=0.1) == j["id"]


def test_cancelling_a_job_does_not_queue_it(client, store, queue):
    p = store.add_project()
    j = store.add_job(p["id"], state="planning")
    client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "cancelled", "actor": "operator"},
        headers=AUTH,
    )
    assert queue.depth() == (0, 0)


def test_a_rejected_transition_queues_nothing(client, store, queue):
    p = store.add_project()
    j = store.add_job(p["id"], state="draft")
    client.post(
        f"/jobs/{j['id']}/transition",
        json={"to": "complete", "actor": "operator"},
        headers=AUTH,
    )
    assert queue.depth() == (0, 0)
