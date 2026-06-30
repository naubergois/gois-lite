"""In-memory kanban schedule jobs with progress (dashboard polling)."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

_MAX_JOBS = 100
_JOB_TTL_SECONDS = 3600.0


@dataclass
class KanbanScheduleJob:
    id: str
    task_id: str
    status: str  # running | done | error
    profile: str = ""
    progress: list[str] = field(default_factory=list)
    last_progress: str = ""
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


_lock = threading.Lock()
_jobs: dict[str, KanbanScheduleJob] = {}


def _prune_locked(now: float) -> None:
    if len(_jobs) <= _MAX_JOBS:
        return
    stale = [
        jid
        for jid, job in _jobs.items()
        if job.finished_at is not None and (now - job.finished_at) > _JOB_TTL_SECONDS
    ]
    for jid in stale:
        _jobs.pop(jid, None)
    if len(_jobs) <= _MAX_JOBS:
        return
    finished = sorted(
        ((jid, j) for jid, j in _jobs.items() if j.finished_at is not None),
        key=lambda x: x[1].finished_at or 0,
    )
    for jid, _ in finished[: max(0, len(_jobs) - _MAX_JOBS)]:
        _jobs.pop(jid, None)


def create_job(task_id: str, *, profile: str = "") -> KanbanScheduleJob:
    job = KanbanScheduleJob(
        id=uuid.uuid4().hex[:16],
        task_id=(task_id or "").strip(),
        profile=(profile or "").strip(),
        status="running",
    )
    with _lock:
        _jobs[job.id] = job
        _prune_locked(time.time())
    return job


def append_progress(job_id: str, message: str) -> None:
    text = (message or "").strip()
    if not text:
        return
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.status != "running":
            return
        job.progress.append(text)
        job.last_progress = text


def complete_job(job_id: str, result: dict[str, Any]) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.status = "done"
        job.result = result
        job.finished_at = time.time()


def fail_job(job_id: str, error: str, *, result: Optional[dict[str, Any]] = None) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.status = "error"
        job.error = error
        job.result = result
        job.finished_at = time.time()


def cancel_job(job_id: str) -> bool:
    """Mark a running kanban schedule job as cancelled."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False
        if job.status != "running":
            return False
        job.status = "cancelled"
        job.error = "Execução cancelada pelo usuário"
        job.finished_at = time.time()
        return True


def is_job_cancelled(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False
        return job.status == "cancelled"


def get_job(job_id: str) -> Optional[KanbanScheduleJob]:
    with _lock:
        job = _jobs.get(job_id)
        return None if job is None else KanbanScheduleJob(**job.__dict__)


def list_running_jobs() -> list[KanbanScheduleJob]:
    with _lock:
        return [
            KanbanScheduleJob(**job.__dict__)
            for job in _jobs.values()
            if job.status == "running"
        ]


def list_recent_jobs() -> list[KanbanScheduleJob]:
    now = time.time()
    with _lock:
        _prune_locked(now)
        return [
            KanbanScheduleJob(**job.__dict__)
            for job in _jobs.values()
            if job.status == "running"
            or job.finished_at is None
            or (now - job.finished_at) <= _JOB_TTL_SECONDS
        ]


def job_to_dict(job: KanbanScheduleJob) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "jobId": job.id,
        "taskId": job.task_id,
        "profile": job.profile,
        "status": job.status,
        "lastProgress": job.last_progress,
        "progress": list(job.progress),
        "startedAt": job.started_at,
    }
    if job.finished_at is not None:
        out["finishedAt"] = job.finished_at
    if job.error:
        out["error"] = job.error
    if job.result is not None:
        out["result"] = job.result
    return out
