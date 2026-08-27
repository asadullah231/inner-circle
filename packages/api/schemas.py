"""
Request and response bodies for the API.

Kept separate from packages/contracts/ on purpose: contracts are the frozen
cross-service shapes (VideoSpec, AssetRecord), these are the HTTP surface and
may change with the API version.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from packages.db.states import Gate, JobState


# --- projects ---------------------------------------------------------------
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    brief: Optional[str] = Field(default=None, max_length=20_000)
    language: str = Field(default="en", max_length=16)
    format_w: int = Field(default=1080, ge=16, le=7680)
    format_h: int = Field(default=1920, ge=16, le=7680)
    fps: int = Field(default=30, ge=1, le=120)


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    brief: Optional[str] = Field(default=None, max_length=20_000)
    language: Optional[str] = Field(default=None, max_length=16)
    format_w: Optional[int] = Field(default=None, ge=16, le=7680)
    format_h: Optional[int] = Field(default=None, ge=16, le=7680)
    fps: Optional[int] = Field(default=None, ge=1, le=120)


class ProjectOut(BaseModel):
    id: UUID
    name: str
    brief: Optional[str] = None
    language: str
    format_w: int
    format_h: int
    fps: int
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# --- jobs -------------------------------------------------------------------
class JobCreate(BaseModel):
    project_id: UUID
    idempotency_key: Optional[str] = Field(default=None, max_length=200)


class ApprovalOut(BaseModel):
    gate: Gate
    decision: str
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    note: Optional[str] = None


class JobOut(BaseModel):
    id: UUID
    project_id: UUID
    state: JobState
    attempt: int
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    #: Set when the job is parked waiting on a person; null while it is moving.
    awaiting_gate: Optional[Gate] = None
    terminal: bool = False
    approvals: list[ApprovalOut] = Field(default_factory=list)


class GateDecision(BaseModel):
    approved: bool
    actor: str = Field(min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=5_000)


class TransitionRequest(BaseModel):
    """Operator override. The worker moves jobs on its own; this is for
    cancelling, and for retrying a failed job back into the pipeline."""

    to: JobState
    actor: str = Field(min_length=1, max_length=200)
    error: Optional[str] = Field(default=None, max_length=5_000)


class TransitionOut(BaseModel):
    job_id: UUID
    frm: JobState
    to: JobState
    changed: bool
    audit_event_id: Optional[int] = None


class AuditEventOut(BaseModel):
    id: int
    event_type: str
    from_state: Optional[JobState] = None
    to_state: Optional[JobState] = None
    actor: Optional[str] = None
    detail: Optional[dict[str, Any]] = None
    created_at: datetime


class HealthOut(BaseModel):
    status: str
    database: str
    auth: str
    version: str
