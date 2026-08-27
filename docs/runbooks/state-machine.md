# Runbook — job state machine

Who this is for: anyone adding a pipeline step, a worker, or an API route that
moves a job.

## The two modules

| Module | Role | Touches the DB |
|---|---|---|
| `packages/db/states.py` | The rules: which moves exist, which need a gate | no |
| `packages/db/transitions.py` | Applies a move, writes the audit trail | yes |

Rules live in `states.py` on purpose: the API and the worker enforce the same
machine, and the rules are testable without a Postgres.

## The flow (PRD 7.2)

```
draft -> planning -> planned --[G1 script]--> retrieving -> retrieved
      --[G2 storyboard]--> rendering -> rendered -> qa -> review
      --[G3 final]--> complete
```

Nine moves end to end. Every working state can also go to `failed` or
`cancelled`. `complete` and `cancelled` are terminal — nothing leaves them.
`failed` is not terminal: an operator can send it back to `planning`,
`retrieving`, or `rendering` (PRD 3.3 — a failure must not force a new job).

Two loop-backs exist for rework: `qa -> rendering` and `review -> rendering`.

## Moving a job

```python
from packages.db import transitions as T
from packages.db.states import JobState

with pool.connection() as conn:          # you own the connection
    T.transition(conn, job_id, JobState.PLANNING, actor="worker-1")
    conn.commit()                        # you own the commit
```

`transition()` never commits. That is deliberate: it takes `SELECT ... FOR
UPDATE` on the job row, and your transaction boundary is what holds that lock.
Commit as late as the unit of work allows, roll back and the state change and
its audit row disappear together.

Return value: `TransitionResult.changed` is `False` when the job was already in
the target state. That is a redelivered message, not an error — do not treat it
as a failure.

Raises:

| Exception | Meaning |
|---|---|
| `IllegalTransition` | the move is not in the machine, or the job is terminal |
| `GateNotApproved` | the move is gated and no approval row says `approved` |
| `JobNotFound` | no such job |

## Approving a gate

```python
from packages.db.states import Gate

T.decide_gate(conn, job_id, Gate.G1_SCRIPT, approved=True, actor="asad")
conn.commit()
```

A decision is not a transition. `decide_gate()` records the human answer; the
worker (or the API) then calls `transition()`, which now sees the approval.
Rejections require a note — enforced here, not only in the UI.

Re-deciding a gate upserts (one row per `job_id, gate`), so a rejected gate can
later be approved and the job continues.

## Adding a new state

1. Add it to the `job_state` enum in a **new** migration (never edit `001`).
2. Add it to `JobState` in `states.py`.
3. Add its row to `_ALLOWED` and add it as a target wherever it is reachable.
4. If it is a human checkpoint, add it to `_GATED` and `AWAITING_GATE`.

`test_states.py` will fail if you miss step 1 or 3: it asserts every state
appears in the SQL enum and that every state is reachable from `draft`.

## Tests

```bash
python -m pytest packages/db          # unit, no database needed
```

Integration tests need a real Postgres and are skipped otherwise:

```bash
RUN_DB_INTEGRATION=1 DATABASE_URL=postgresql://... python -m pytest packages/db -m db_integration
```

CI runs them in the `core-db-integration` job against `postgres:16`. The
concurrency test (two connections racing the same job) is the reason that job
exists — row locking cannot be proven against a fake connection.
