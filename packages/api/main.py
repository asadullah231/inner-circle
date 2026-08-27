"""
Inner Circle control-plane API (M1.3).

Creates projects and jobs, exposes job status, and records the three human
gate decisions. It does no production work: the worker (M1.4) advances jobs,
and every move goes through packages.db.transitions so the state machine is
the single authority on what is legal.

Run locally:
    DATABASE_URL=postgresql://... API_TOKEN=dev-token \
        uvicorn packages.api.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from packages.db import transitions
from packages.db.states import (
    Gate,
    GateNotApproved,
    IllegalTransition,
    JobState,
    is_terminal,
)
from packages.db.transitions import JobNotFound
from packages.worker.queue import MemoryQueue, Queue, RedisQueue
from packages.worker.runner import resume_after_gate

from . import repository as repo
from .config import Settings, load
from .deps import Database, get_db, get_queue, get_settings, require_token, txn
from .schemas import (
    AuditEventOut,
    GateDecision,
    HealthOut,
    JobCreate,
    JobOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    TransitionOut,
    TransitionRequest,
)

log = logging.getLogger("api")

VERSION = "0.3.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    db: Database = app.state.db
    if not db.is_open:
        db.open()
    if settings.auto_migrate:
        from packages.db import migrate

        with db.connection() as conn:
            applied = migrate.run(conn)
            conn.commit()
        log.info("migrations applied: %s", applied or "none pending")
    if not settings.auth_enabled:
        log.warning("API_TOKEN is not set - authentication is DISABLED")
    yield
    db.close()


def create_app(
    settings: Optional[Settings] = None,
    db: Optional[Database] = None,
    queue: Optional[Queue] = None,
) -> FastAPI:
    """Factory so tests can inject a settings/database/queue triple."""
    settings = settings or load()
    app = FastAPI(
        title="Inner Circle API",
        version=VERSION,
        summary="Control plane for the automated video generator",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = db or Database(
        settings.database_url, min_size=settings.pool_min, max_size=settings.pool_max
    )
    app.state.queue = queue if queue is not None else _build_queue(settings)
    _register(app)
    return app


def _build_queue(settings: Settings) -> Queue:
    """Redis when configured, in-process otherwise.

    The in-process fallback keeps `uvicorn packages.api.main:app` working with
    nothing but a database, but a worker in another process will never see
    those pushes — hence the warning.
    """
    if settings.redis_url:
        return RedisQueue(settings.redis_url)
    log.warning("REDIS_URL is not set - using an in-process queue (dev only)")
    return MemoryQueue()


def _problem(code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"detail": detail})


def _register(app: FastAPI) -> None:
    """Attach every handler. Called by create_app so each app instance is
    independent — the tests build their own without touching the module one."""

    # --- error translation --------------------------------------------------
    @app.exception_handler(IllegalTransition)
    async def _illegal(_, exc: IllegalTransition):
        return _problem(status.HTTP_409_CONFLICT, str(exc))

    @app.exception_handler(GateNotApproved)
    async def _not_approved(_, exc: GateNotApproved):
        return _problem(status.HTTP_403_FORBIDDEN, str(exc))

    @app.exception_handler(JobNotFound)
    async def _no_job(_, exc: JobNotFound):
        return _problem(status.HTTP_404_NOT_FOUND, str(exc))

    @app.exception_handler(repo.ProjectNotFound)
    async def _no_project(_, exc: repo.ProjectNotFound):
        return _problem(status.HTTP_404_NOT_FOUND, str(exc))

    # --- health -------------------------------------------------------------
    @app.get("/health", response_model=HealthOut, tags=["ops"])
    def health(
        settings: Settings = Depends(get_settings), db: Database = Depends(get_db)
    ) -> HealthOut:
        """Unauthenticated: a load balancer must be able to call it.

        Reports whether auth is on, so an accidentally open deploy is visible
        rather than silent.
        """
        reachable = db.ping()
        return HealthOut(
            status="ok" if reachable else "degraded",
            database="up" if reachable else "down",
            auth="enabled" if settings.auth_enabled else "disabled",
            version=VERSION,
        )

    # --- projects -----------------------------------------------------------
    @app.post(
        "/projects",
        response_model=ProjectOut,
        status_code=status.HTTP_201_CREATED,
        tags=["projects"],
    )
    def create_project(
        body: ProjectCreate, conn=Depends(txn), actor: str = Depends(require_token)
    ) -> Any:
        return repo.create_project(conn, created_by=actor, **body.model_dump())

    @app.get("/projects", response_model=list[ProjectOut], tags=["projects"])
    def list_projects(
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        conn=Depends(txn),
        _: str = Depends(require_token),
    ) -> Any:
        return repo.list_projects(conn, limit=limit, offset=offset)

    @app.get("/projects/{project_id}", response_model=ProjectOut, tags=["projects"])
    def get_project(
        project_id: UUID, conn=Depends(txn), _: str = Depends(require_token)
    ) -> Any:
        return repo.get_project(conn, project_id)

    @app.patch("/projects/{project_id}", response_model=ProjectOut, tags=["projects"])
    def update_project(
        project_id: UUID,
        body: ProjectUpdate,
        conn=Depends(txn),
        _: str = Depends(require_token),
    ) -> Any:
        return repo.update_project(conn, project_id, **body.model_dump(exclude_unset=True))

    @app.delete(
        "/projects/{project_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["projects"],
    )
    def delete_project(
        project_id: UUID, conn=Depends(txn), _: str = Depends(require_token)
    ) -> Response:
        if not repo.delete_project(conn, project_id):
            raise repo.ProjectNotFound(project_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # --- jobs ---------------------------------------------------------------
    @app.post("/jobs", response_model=JobOut, tags=["jobs"])
    def create_job(
        body: JobCreate,
        response: Response,
        conn=Depends(txn),
        queue: Queue = Depends(get_queue),
        _: str = Depends(require_token),
    ) -> Any:
        """
        Create a job in `draft` and hand it to the worker.

        Replaying an `idempotency_key` returns the original job with 200
        instead of 201 and does NOT queue it again, so a client retry never
        starts a second production run.
        """
        job, created = repo.create_job(
            conn, project_id=body.project_id, idempotency_key=body.idempotency_key
        )
        response.status_code = (
            status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )
        out = _job_out(conn, job)
        if created:
            queue.push(str(job["id"]))
        return out

    @app.get("/jobs", response_model=list[JobOut], tags=["jobs"])
    def list_jobs(
        project_id: Optional[UUID] = None,
        state: Optional[JobState] = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        conn=Depends(txn),
        _: str = Depends(require_token),
    ) -> Any:
        jobs = repo.list_jobs(
            conn, project_id=project_id, state=state, limit=limit, offset=offset
        )
        return [_job_out(conn, j) for j in jobs]

    @app.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"])
    def get_job(job_id: UUID, conn=Depends(txn), _: str = Depends(require_token)) -> Any:
        job = repo.get_job(conn, job_id)
        if job is None:
            raise JobNotFound(job_id)
        return _job_out(conn, job)

    @app.get("/jobs/{job_id}/history", response_model=list[AuditEventOut], tags=["jobs"])
    def job_history(
        job_id: UUID,
        limit: int = Query(100, ge=1, le=1000),
        conn=Depends(txn),
        _: str = Depends(require_token),
    ) -> Any:
        if repo.get_job(conn, job_id) is None:
            raise JobNotFound(job_id)
        return transitions.history(conn, job_id, limit=limit)

    # --- gates --------------------------------------------------------------
    @app.post("/jobs/{job_id}/gates/{gate}", response_model=JobOut, tags=["gates"])
    def decide_gate(
        job_id: UUID,
        gate: Gate,
        body: GateDecision,
        conn=Depends(txn),
        queue: Queue = Depends(get_queue),
        _: str = Depends(require_token),
    ) -> Any:
        """
        Record a human decision at a gate (PRD FR-3).

        This does not advance the job itself. It records the decision and, on
        an approval, puts the job back on the queue — the worker does the
        move, and finds the approval waiting. That is why a parked job needs
        no polling.

        A rejection requires a note, enforced in transitions.decide_gate() so
        no endpoint can bypass it.
        """
        if repo.get_job(conn, job_id) is None:
            raise JobNotFound(job_id)
        try:
            transitions.decide_gate(
                conn,
                job_id,
                gate,
                approved=body.approved,
                actor=body.actor,
                note=body.note,
            )
        except ValueError as exc:
            raise HTTPException(
                422, str(exc)
            ) from exc

        job = repo.get_job(conn, job_id)
        out = _job_out(conn, job)  # type: ignore[arg-type]
        if body.approved:
            resume_after_gate(queue, job_id)
        return out

    # --- operator transitions -----------------------------------------------
    @app.post("/jobs/{job_id}/transition", response_model=TransitionOut, tags=["jobs"])
    def transition_job(
        job_id: UUID,
        body: TransitionRequest,
        conn=Depends(txn),
        queue: Queue = Depends(get_queue),
        _: str = Depends(require_token),
    ) -> Any:
        """
        Operator override: cancel a job, or retry a failed one.

        The worker drives the happy path; this exists for the cases a human
        has to unstick. It goes through the same state machine, so an illegal
        move is a 409 and an unapproved gate is a 403.
        """
        result = transitions.transition(
            conn, job_id, body.to, actor=body.actor, error=body.error
        )
        # A job an operator put back into the pipeline needs a worker again.
        if result.changed and not is_terminal(body.to):
            queue.push(str(job_id))
        return TransitionOut(
            job_id=UUID(result.job_id),
            frm=result.frm,
            to=result.to,
            changed=result.changed,
            audit_event_id=result.audit_event_id,
        )


def _job_out(conn, job: dict[str, Any]) -> dict[str, Any]:
    """Decorate a job row with the two things a caller always needs next:
    which gate it is parked on, and whether it can still move."""
    state = JobState(job["state"])
    return {
        **job,
        "awaiting_gate": transitions.awaiting_gate(state),
        "terminal": is_terminal(state),
        "approvals": repo.get_approvals(conn, job["id"]),
    }


app = create_app()
