# Runbook — API and worker

The M1 control plane: an HTTP API that owns projects, jobs and gate decisions,
and a worker that advances jobs through the state machine. Neither does any
production work yet — the planner, media retrieval and render land in M2, M3
and M4.

## Running it

Both processes need the same `DATABASE_URL`. They only find each other through
the database and the queue, never directly.

```bash
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/innercircle
export API_TOKEN=$(openssl rand -hex 24)
export REDIS_URL=redis://localhost:6379/0

# once, to create the schema
python -c "import psycopg; from packages.db import migrate; \
  c = psycopg.connect(); print(migrate.run(c)); c.commit()"

# terminal 1
uvicorn packages.api.main:app --host 0.0.0.0 --port 8000

# terminal 2
python -m packages.worker.runner
```

`pip install -r packages/api/requirements.txt` covers both.

### Environment

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_URL` | libpq defaults | Postgres connection string |
| `API_TOKEN` | *(empty)* | Bearer token. **Empty disables auth** — `/health` reports `auth: disabled` |
| `REDIS_URL` | *(empty)* | Empty means an in-process queue: the worker will never see the API's pushes |
| `DB_POOL_MIN` / `DB_POOL_MAX` | 1 / 8 | Connection pool size |
| `API_AUTO_MIGRATE` | `0` | Run pending migrations on API startup |
| `WORKER_NAME` | `worker-1` | Appears as the actor on every audit row |
| `LOG_LEVEL` | `INFO` | Worker log level |

Two settings are easy to get wrong and both are visible in `/health` or the
startup log rather than silent: no `API_TOKEN` leaves the API open, and no
`REDIS_URL` leaves the API and worker with separate queues so nothing moves.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | No auth. Reports database reachability and whether auth is on |
| `POST` | `/projects` | |
| `GET` | `/projects` `/projects/{id}` | |
| `PATCH` | `/projects/{id}` | Partial update |
| `DELETE` | `/projects/{id}` | Cascades to its jobs and their history |
| `POST` | `/jobs` | Creates in `draft` and queues it. `idempotency_key` replays return 200 |
| `GET` | `/jobs` | Filter by `project_id`, `state` |
| `GET` | `/jobs/{id}` | Includes `awaiting_gate`, `terminal`, and the approvals so far |
| `GET` | `/jobs/{id}/history` | The audit trail |
| `POST` | `/jobs/{id}/gates/{gate}` | Record a human decision. An approval re-queues the job |
| `POST` | `/jobs/{id}/transition` | Operator override: cancel, or retry a failed job |

Interactive docs at `/docs` once the API is up.

### Status codes that mean something specific

| Code | Meaning |
|---|---|
| `409` | The state machine refused the move. The job is unchanged |
| `403` | The move is legal but its gate has no approval |
| `422` | Validation, including a gate rejection sent without a note |
| `200` on `POST /jobs` | An `idempotency_key` replay — the original job, not a new one |

## The walkthrough

```bash
API=http://localhost:8000
H="Authorization: Bearer $API_TOKEN"

PROJECT=$(curl -sX POST $API/projects -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Solar panels explainer"}' | jq -r .id)

JOB=$(curl -sX POST $API/jobs -H "$H" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJECT\"}" | jq -r .id)

# the worker runs; the job stops at the first gate
curl -s $API/jobs/$JOB -H "$H" | jq '{state, awaiting_gate}'
# => { "state": "planned", "awaiting_gate": "g1_script" }

curl -sX POST $API/jobs/$JOB/gates/g1_script -H "$H" -H 'Content-Type: application/json' \
  -d '{"approved":true,"actor":"asad"}' | jq .state

# repeat for g2_storyboard and g3_final, then
curl -s $API/jobs/$JOB/history -H "$H" | jq -r '.[] | "\(.from_state) -> \(.to_state) \(.actor)"'
```

No `jq` and no server: `python demo_m1_full.py` does the same thing in-process.

## How a job moves

The worker never decides policy. It reads the job's state, asks the state
machine what comes next, and writes the move inside one transaction that holds
the job's row lock.

```
draft ──► planning ──► planned ──[G1]──► retrieving ──► retrieved ──[G2]──►
rendering ──► rendered ──► qa ──► review ──[G3]──► complete
```

Nine transitions on the happy path. Three of them need a human first.

**A parked job is not a failure.** At `planned`, `retrieved` and `review` the
worker acks the message and walks away — nothing polls. The job comes back on
the queue when someone approves, because `POST /gates/{gate}` pushes it. This
is the piece to remember when debugging "the job is stuck": check the approval
before you check the worker.

## Operations

### The job is not moving

1. `GET /jobs/{id}` — an `awaiting_gate` means it is waiting on a person, not broken
2. `GET /health` — `database: down` explains everything else
3. Worker log — `parked` is normal, repeated `raised` is not
4. Redis: `LLEN ic:jobs:pending` and `LLEN ic:jobs:processing`. Entries stuck
   in `processing` mean a worker died holding them

### A worker died holding jobs

Restarting it is the fix. `Worker.run()` calls `recover()` first, which moves
everything from `processing` back to `pending`.

With more than one worker this is blunt: it recovers live workers' jobs too. The
state machine makes that safe (the second attempt is a no-op or an illegal
transition, never a double render), but it wastes a run. With a fleet, recover
from one operator command while the others are down rather than on every boot.

### Retrying a failed job

```bash
curl -sX POST $API/jobs/$JOB/transition -H "$H" -H 'Content-Type: application/json' \
  -d '{"to":"planning","actor":"asad"}'
```

Legal from `failed` back to `planning`, `retrieving` or `rendering` — put it
back at the step that broke. The stale error clears on the way. `complete` and
`cancelled` are final and a retry gets a 409.

### Cancelling

```bash
curl -sX POST $API/jobs/$JOB/transition -H "$H" -H 'Content-Type: application/json' \
  -d '{"to":"cancelled","actor":"asad"}'
```

Legal from every non-terminal state. A worker that later picks the job up sees
a terminal state and drops it.

## Testing

```bash
pytest packages/api packages/worker          # no database needed
RUN_DB_INTEGRATION=1 DATABASE_URL=... \
  pytest packages/worker/tests -m db_integration    # the M1 acceptance tests
```

The integration suite is the milestone's acceptance criteria: create through
the API, drive with a worker, kill it mid-job, restart, and check that no state
was lost or replayed. It also races two workers at one job to prove the row
lock holds.

## Known limits at M1

- One shared bearer token, not user accounts. Real auth is M6
- `recover()` is per-worker, as above
- The worker's steps are stubs. `_STEPS` in `runner.py` is the map M2, M3 and
  M4 replace one entry at a time
- No rate limiting or request size limits on the API
